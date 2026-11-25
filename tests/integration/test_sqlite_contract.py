"""Real disk SQLite contract tests (M1.4): PRAGMAs, FKs, partial uniques, idempotency, FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from video_agent.adapters.storage.connection import (
    connect_sqlite,
    create_analysis_with_job,
    create_question_with_job,
    immediate_transaction,
    publish_answer_and_finish_job,
)
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.domain.errors import ErrorCode, StorageError

VALID_SHA = "a" * 64
TS = "2026-11-25T10:00:00Z"


def _seed_ready_asset(conn: sqlite3.Connection, asset_id: str, art_id: str) -> None:
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO assets (
                asset_id, source_artifact_id, sha256, size_bytes, duration_ms,
                width, height, video_codec, has_audio, audio_codec,
                display_filename, status, created_at
            ) VALUES (
                ?, NULL, ?, 1024, 60000, 1920, 1080, 'h264', 1, 'aac', 'v.mp4', 'importing', ?
            )
            """,
            (asset_id, VALID_SHA, TS),
        )
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, asset_id, kind, relative_path, sha256, size_bytes, created_at
            ) VALUES (?, ?, 'source_video', ?, ?, 1024, ?)
            """,
            (art_id, asset_id, f"assets/{asset_id}/source.mp4", VALID_SHA, TS),
        )
        conn.execute(
            "UPDATE assets SET source_artifact_id = ?, status = 'ready' WHERE asset_id = ?",
            (art_id, asset_id),
        )


def test_connection_factory_enforces_pragmas_before_begin_on_bootstrap_and_subsequent(
    tmp_path: Path,
) -> None:
    """Both bootstrap and subsequent connections verify FK, busy_timeout, WAL before BEGIN."""
    db_path = tmp_path / "agent.db"
    trace_log: list[str] = []

    conn1 = connect_sqlite(db_path, bootstrap=True, trace=trace_log.append)
    try:
        assert conn1.in_transaction is False
        assert conn1.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn1.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert str(conn1.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

        with immediate_transaction(conn1):
            conn1.execute("CREATE TABLE _probe (id INTEGER PRIMARY KEY)")
    finally:
        conn1.close()

    fk_idx = next(i for i, sql in enumerate(trace_log) if "PRAGMA foreign_keys = ON" in sql)
    busy_idx = next(i for i, sql in enumerate(trace_log) if "PRAGMA busy_timeout = 5000" in sql)
    begin_idx = next(i for i, sql in enumerate(trace_log) if "BEGIN IMMEDIATE" in sql)
    assert fk_idx < begin_idx
    assert busy_idx < begin_idx

    trace2: list[str] = []
    conn2 = connect_sqlite(db_path, bootstrap=False, trace=trace2.append)
    try:
        assert conn2.in_transaction is False
        assert conn2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn2.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert str(conn2.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        conn2.close()


def test_non_bootstrap_connection_rejects_missing_or_non_wal_database(tmp_path: Path) -> None:
    """Normal connect_sqlite refuses to auto-create missing DB or accept non-WAL DB."""
    missing = tmp_path / "missing.db"
    with pytest.raises(StorageError) as exc_missing:
        connect_sqlite(missing, bootstrap=False)
    assert exc_missing.value.code is ErrorCode.STORAGE_ERROR
    assert not missing.exists()

    plain_db = tmp_path / "delete_mode.db"
    raw = sqlite3.connect(plain_db)
    raw.execute("PRAGMA journal_mode = DELETE")
    raw.execute("CREATE TABLE t (x INT)")
    raw.commit()
    raw.close()

    with pytest.raises(StorageError) as exc_wal:
        connect_sqlite(plain_db, bootstrap=False)
    assert exc_wal.value.code is ErrorCode.STORAGE_ERROR


def test_relational_fks_block_cross_asset_and_invalid_state_writes(tmp_path: Path) -> None:
    """Real disk SQLite enforces asset/source_video, cross-asset evidence, and job/answer FKs."""
    db_path = tmp_path / "app.db"
    run_migrations(db_path, data_root=tmp_path, writers_stopped=True)

    conn = connect_sqlite(db_path)
    try:
        _seed_ready_asset(conn, "asset_1", "art_src_1")
        _seed_ready_asset(conn, "asset_2", "art_src_2")

        # Ready asset without source_artifact_id fails CHECK
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO assets (
                    asset_id, source_artifact_id, sha256, size_bytes, duration_ms,
                    width, height, video_codec, has_audio, audio_codec,
                    display_filename, status, created_at
                ) VALUES (
                    'asset_bad', NULL, ?, 100, 1000, 640, 480, 'h264', 0, NULL, 'x.mp4', 'ready', ?
                )
                """,
                (VALID_SHA, TS),
            )

        # Binding another asset's source_video artifact fails composite FK
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                "UPDATE assets SET source_artifact_id = 'art_src_2' WHERE asset_id = 'asset_1'"
            )

        # Binding a frame artifact as source_artifact_id fails composite FK
        with immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, asset_id, kind, relative_path, sha256, size_bytes, created_at
                ) VALUES ('art_frame_1', 'asset_1', 'frame', 'frames/f1.jpg', ?, 512, ?)
                """,
                (VALID_SHA, TS),
            )
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, asset_id, kind, relative_path, sha256, size_bytes, created_at
                ) VALUES ('art_frame_2', 'asset_2', 'frame', 'frames/f2.jpg', ?, 512, ?)
                """,
                (VALID_SHA, TS),
            )
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                "UPDATE assets SET source_artifact_id = 'art_frame_1' WHERE asset_id = 'asset_1'"
            )

        # Create valid analysis_run, evidence, and observation on asset_1; evidence on asset_2
        create_analysis_with_job(
            conn,
            analysis_id="analysis_1",
            asset_id="asset_1",
            job_id="job_an_1",
            config_hash="cfg_1",
            prompt_bundle_hash="pb_1",
            created_at=TS,
        )
        with immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, asset_id, kind, point_ms, start_ms, end_ms,
                    artifact_id, payload, created_at
                ) VALUES ('ev_1', 'asset_1', 'frame', 1000, NULL, NULL, 'art_frame_1', '{}', ?)
                """,
                (TS,),
            )
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, asset_id, kind, point_ms, start_ms, end_ms,
                    artifact_id, payload, created_at
                ) VALUES ('ev_2', 'asset_2', 'frame', 1000, NULL, NULL, 'art_frame_2', '{}', ?)
                """,
                (TS,),
            )
            conn.execute(
                """
                INSERT INTO observations (
                    observation_id, analysis_id, asset_id, kind, text,
                    start_ms, end_ms, region_hint, provenance, created_at
                ) VALUES (
                    'obs_1', 'analysis_1', 'asset_1', 'observed', 'text',
                    0, 2000, NULL, '{}', ?
                )
                """,
                (TS,),
            )

        # Cross-asset citation in observation_evidence fails composite FK!
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO observation_evidence (observation_id, evidence_id, asset_id)
                VALUES ('obs_1', 'ev_2', 'asset_1')
                """
            )

        # Duplicate analysis job for same analysis_id fails partial unique index
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, asset_id, analysis_id, question_id, kind, status,
                    cancel_requested, attempt_no, created_at, updated_at
                ) VALUES (
                    'job_an_dup', 'asset_1', 'analysis_1', NULL, 'analysis',
                    'queued', 0, 0, ?, ?
                )
                """,
                (TS, TS),
            )

        # Frame evidence pretending to be a range fails CHECK
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, asset_id, kind, point_ms, start_ms, end_ms,
                    artifact_id, payload, created_at
                ) VALUES (
                    'ev_bad_frame', 'asset_1', 'frame', 1000, 1000, 1000,
                    'art_frame_1', '{}', ?
                )
                """,
                (TS,),
            )
    finally:
        conn.close()


def test_atomic_analysis_question_answer_transactions_and_idempotency(tmp_path: Path) -> None:
    """Analysis+job, question+job, and answer+job finish commit atomically; conflicts fail."""
    db_path = tmp_path / "atomic.db"
    run_migrations(db_path, data_root=tmp_path, writers_stopped=True)

    conn = connect_sqlite(db_path)
    try:
        _seed_ready_asset(conn, "asset_1", "art_src_1")

        a_id, j_id = create_analysis_with_job(
            conn,
            analysis_id="analysis_1",
            asset_id="asset_1",
            job_id="job_an_1",
            config_hash="cfg_1",
            prompt_bundle_hash="pb_1",
            created_at=TS,
            idempotency_key="idem-an-1",
            idempotency_payload_hash="hash_v1",
        )
        assert (a_id, j_id) == ("analysis_1", "job_an_1")

        # Same idempotency key + same payload hash returns existing IDs idempotently
        a_id2, j_id2 = create_analysis_with_job(
            conn,
            analysis_id="analysis_new",
            asset_id="asset_1",
            job_id="job_an_new",
            config_hash="cfg_1",
            prompt_bundle_hash="pb_1",
            created_at=TS,
            idempotency_key="idem-an-1",
            idempotency_payload_hash="hash_v1",
        )
        assert (a_id2, j_id2) == ("analysis_1", "job_an_1")

        # Same idempotency key + different payload hash raises IDEMPOTENCY_CONFLICT
        with pytest.raises(StorageError) as conflict_an:
            create_analysis_with_job(
                conn,
                analysis_id="analysis_conflict",
                asset_id="asset_1",
                job_id="job_an_conflict",
                config_hash="cfg_2",
                prompt_bundle_hash="pb_1",
                created_at=TS,
                idempotency_key="idem-an-1",
                idempotency_payload_hash="hash_v2",
            )
        assert conflict_an.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

        q_id, qa_job_id = create_question_with_job(
            conn,
            question_id="q_1",
            asset_id="asset_1",
            analysis_id="analysis_1",
            job_id="job_qa_1",
            query_text="在哪修改了孔径？",
            mode="qa",
            created_at=TS,
            idempotency_key="idem-q-1",
            idempotency_payload_hash="qhash_1",
        )
        assert (q_id, qa_job_id) == ("q_1", "job_qa_1")

        with pytest.raises(StorageError) as conflict_q:
            create_question_with_job(
                conn,
                question_id="q_conflict",
                asset_id="asset_1",
                analysis_id="analysis_1",
                job_id="job_qa_conflict",
                query_text="不同问题",
                mode="qa",
                created_at=TS,
                idempotency_key="idem-q-1",
                idempotency_payload_hash="qhash_2",
            )
        assert conflict_q.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

        # Duplicate QA job for same question_id fails partial unique index
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, asset_id, analysis_id, question_id, kind, status,
                    cancel_requested, attempt_no, created_at, updated_at
                ) VALUES ('job_qa_dup', 'asset_1', 'analysis_1', 'q_1', 'qa', 'queued', 0, 0, ?, ?)
                """,
                (TS, TS),
            )

        # Publish answer and finish QA job atomically
        publish_answer_and_finish_job(
            conn,
            answer_id="ans_1",
            question_id="q_1",
            analysis_id="analysis_1",
            asset_id="asset_1",
            job_id="job_qa_1",
            answer_status="answered",
            job_status="succeeded",
            payload_json='{"status":"answered"}',
            trace_id="trace_1",
            finished_at=TS,
        )
        ans_row = conn.execute("SELECT status FROM answers WHERE question_id = 'q_1'").fetchone()
        job_row = conn.execute("SELECT status FROM jobs WHERE job_id = 'job_qa_1'").fetchone()
        assert ans_row == ("answered",)
        assert job_row == ("succeeded",)

        # Second answer for same question_id violates UNIQUE(question_id)
        with pytest.raises(StorageError):
            publish_answer_and_finish_job(
                conn,
                answer_id="ans_dup",
                question_id="q_1",
                analysis_id="analysis_1",
                asset_id="asset_1",
                job_id="job_qa_1",
                answer_status="answered",
                job_status="succeeded",
                payload_json='{"status":"answered"}',
                trace_id="trace_2",
                finished_at=TS,
            )

        # Verify FTS5 search_docs + search_fts roundtrip
        with immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO search_docs (
                    doc_id, analysis_id, asset_id, source_kind, source_id,
                    original_text, tokens, start_ms, end_ms, index_version, created_at
                ) VALUES ('doc_1', 'analysis_1', 'asset_1', 'observation', 'obs_1',
                          '修改孔径', '修改 改孔 孔径', 0, 2000, 'idx-v1', ?)
                """,
                (TS,),
            )
            conn.execute(
                "INSERT INTO search_fts (doc_id, tokens) VALUES ('doc_1', '修改 改孔 孔径')"
            )
        hits = conn.execute(
            "SELECT doc_id FROM search_fts WHERE search_fts MATCH ?",
            ("孔径",),
        ).fetchall()
        assert hits == [("doc_1",)]
    finally:
        conn.close()


def test_publish_answer_requires_terminal_job_status_and_disk_rejects_float_ms(
    tmp_path: Path,
) -> None:
    """M1.4 regression: answer publish rejects non-terminal status; disk schema rejects float ms."""
    db_path = tmp_path / "strict_disk.db"
    run_migrations(db_path, data_root=tmp_path, writers_stopped=True)

    conn = connect_sqlite(db_path)
    try:
        _seed_ready_asset(conn, "asset_1", "art_src_1")
        create_analysis_with_job(
            conn,
            analysis_id="analysis_1",
            asset_id="asset_1",
            job_id="job_an_1",
            config_hash="cfg_1",
            prompt_bundle_hash="pb_1",
            created_at=TS,
        )
        create_question_with_job(
            conn,
            question_id="q_1",
            asset_id="asset_1",
            analysis_id="analysis_1",
            job_id="job_qa_1",
            query_text="测试终态",
            mode="qa",
            created_at=TS,
        )
        with immediate_transaction(conn):
            conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = 'job_qa_1'")
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, asset_id, kind, relative_path, sha256, size_bytes, created_at
                ) VALUES ('art_frame_1', 'asset_1', 'frame', 'frames/f1.jpg', ?, 512, ?)
                """,
                (VALID_SHA, TS),
            )

        # Publishing an answer with non-terminal job_status ('running' or 'queued') MUST fail
        for bad_status in ("running", "queued"):
            with pytest.raises(StorageError) as exc_status:
                publish_answer_and_finish_job(
                    conn,
                    answer_id=f"ans_{bad_status}",
                    question_id="q_1",
                    analysis_id="analysis_1",
                    asset_id="asset_1",
                    job_id="job_qa_1",
                    answer_status="answered",
                    job_status=bad_status,
                    payload_json='{"status":"answered"}',
                    trace_id="trace_bad",
                    finished_at=TS,
                )
            assert exc_status.value.code is ErrorCode.STORAGE_ERROR

        # Disk evidence/observations tables MUST reject floating-point milliseconds (e.g., 1.5)
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, asset_id, kind, point_ms, start_ms, end_ms,
                    artifact_id, payload, created_at
                ) VALUES (
                    'ev_float_tr', 'asset_1', 'transcript', NULL, 1.5, 100,
                    NULL, '{}', ?
                )
                """,
                (TS,),
            )

        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, asset_id, kind, point_ms, start_ms, end_ms,
                    artifact_id, payload, created_at
                ) VALUES (
                    'ev_float_frame', 'asset_1', 'frame', 1.5, NULL, NULL,
                    'art_frame_1', '{}', ?
                )
                """,
                (TS,),
            )

        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO observations (
                    observation_id, analysis_id, asset_id, kind, text,
                    start_ms, end_ms, region_hint, provenance, created_at
                ) VALUES (
                    'obs_float', 'analysis_1', 'asset_1', 'observed', 'float ms',
                    1.5, 100, NULL, '{}', ?
                )
                """,
                (TS,),
            )
    finally:
        conn.close()

    # Re-open from disk: NO answer was leaked while job remained running
    reopened = connect_sqlite(db_path)
    try:
        assert reopened.execute("SELECT COUNT(*) FROM answers").fetchone() == (0,)
        assert reopened.execute("SELECT status FROM jobs WHERE job_id = 'job_qa_1'").fetchone() == (
            "running",
        )
    finally:
        reopened.close()
