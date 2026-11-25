"""Unified SQLite connection factory and atomic transaction helpers (M1.4).

Every connection verifies foreign_keys=ON, busy_timeout=5000, and WAL mode BEFORE
any transaction begins, using explicit isolation_level=None.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from video_agent.domain.errors import ErrorCode, StorageError


def connect_sqlite(
    path: Path,
    *,
    bootstrap: bool = False,
    trace: Callable[[str], None] | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection and enforce foreign_keys, busy_timeout, and WAL before BEGIN."""
    if not bootstrap and not path.is_file():
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            "SQLite database does not exist; run migrations before opening normal connections",
        )
    if bootstrap:
        path.parent.mkdir(parents=True, exist_ok=True)
        target = str(path)
    else:
        target = path.resolve().as_uri() + "?mode=rw"

    try:
        conn = sqlite3.connect(
            target,
            uri=not bootstrap,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise StorageError(ErrorCode.STORAGE_ERROR, f"failed to open SQLite DB: {exc}") from exc

    try:
        if trace is not None:
            conn.set_trace_callback(trace)
        if conn.in_transaction:
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                "SQLite connection must be configured before BEGIN",
            )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if bootstrap:
            journal_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        else:
            journal_row = conn.execute("PRAGMA journal_mode").fetchone()

        fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
        busy_row = conn.execute("PRAGMA busy_timeout").fetchone()

        if fk_row is None or int(fk_row[0]) != 1:
            raise StorageError(ErrorCode.STORAGE_ERROR, "SQLite foreign_keys is not enabled")
        if busy_row is None or int(busy_row[0]) != 5000:
            raise StorageError(ErrorCode.STORAGE_ERROR, "SQLite busy_timeout mismatch")
        if journal_row is None or str(journal_row[0]).lower() != "wal":
            raise StorageError(ErrorCode.STORAGE_ERROR, "SQLite WAL mode is required")
        if conn.in_transaction:
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                "SQLite connection unexpectedly entered a transaction during setup",
            )
        return conn
    except StorageError:
        conn.close()
        raise
    except BaseException as exc:
        conn.close()
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            f"SQLite connection configuration failed: {exc}",
        ) from exc


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run statements inside an explicit BEGIN IMMEDIATE ... COMMIT/ROLLBACK transaction."""
    if conn.in_transaction:
        raise StorageError(ErrorCode.STORAGE_ERROR, "nested transaction is not allowed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def create_analysis_with_job(
    conn: sqlite3.Connection,
    *,
    analysis_id: str,
    asset_id: str,
    job_id: str,
    config_hash: str,
    prompt_bundle_hash: str,
    created_at: str,
    idempotency_key: str | None = None,
    idempotency_payload_hash: str | None = None,
) -> tuple[str, str]:
    """Atomically insert analysis_run and its unique analysis job with idempotency checking."""
    scope = f"asset:{asset_id}" if idempotency_key is not None else None
    if (idempotency_key is None) != (idempotency_payload_hash is None):
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            "idempotency_key and idempotency_payload_hash must be provided together",
        )

    with immediate_transaction(conn):
        if idempotency_key is not None:
            existing = conn.execute(
                """
                SELECT analysis_id, job_id, idempotency_payload_hash
                FROM jobs
                WHERE kind = 'analysis'
                  AND idempotency_scope = ?
                  AND idempotency_key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing[2] != idempotency_payload_hash:
                    raise StorageError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key reused with a different analysis payload",
                    )
                return str(existing[0]), str(existing[1])

        conn.execute(
            """
            INSERT INTO analysis_runs (
                analysis_id, asset_id, config_hash, prompt_bundle_hash,
                index_state, status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', 'queued', ?)
            """,
            (analysis_id, asset_id, config_hash, prompt_bundle_hash, created_at),
        )
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, asset_id, analysis_id, question_id, kind, status,
                cancel_requested, attempt_no, idempotency_scope, idempotency_key,
                idempotency_payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 'analysis', 'queued', 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                asset_id,
                analysis_id,
                scope,
                idempotency_key,
                idempotency_payload_hash,
                created_at,
                created_at,
            ),
        )
    return analysis_id, job_id


def create_question_with_job(
    conn: sqlite3.Connection,
    *,
    question_id: str,
    asset_id: str,
    analysis_id: str,
    job_id: str,
    query_text: str,
    mode: str,
    created_at: str,
    idempotency_key: str | None = None,
    idempotency_payload_hash: str | None = None,
) -> tuple[str, str]:
    """Atomically insert question and its unique QA job with idempotency checking."""
    scope = f"analysis:{analysis_id}" if idempotency_key is not None else None
    if (idempotency_key is None) != (idempotency_payload_hash is None):
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            "idempotency_key and idempotency_payload_hash must be provided together",
        )

    with immediate_transaction(conn):
        if idempotency_key is not None:
            existing = conn.execute(
                """
                SELECT question_id, job_id, idempotency_payload_hash
                FROM jobs
                WHERE kind = 'qa'
                  AND idempotency_scope = ?
                  AND idempotency_key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing[2] != idempotency_payload_hash:
                    raise StorageError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key reused with a different question payload",
                    )
                return str(existing[0]), str(existing[1])

        conn.execute(
            """
            INSERT INTO questions (
                question_id, asset_id, analysis_id, query_text, mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (question_id, asset_id, analysis_id, query_text, mode, created_at),
        )
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, asset_id, analysis_id, question_id, kind, status,
                cancel_requested, attempt_no, idempotency_scope, idempotency_key,
                idempotency_payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'qa', 'queued', 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                asset_id,
                analysis_id,
                question_id,
                scope,
                idempotency_key,
                idempotency_payload_hash,
                created_at,
                created_at,
            ),
        )
    return question_id, job_id


_TERMINAL_QA_JOB_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "partial", "failed", "cancelled"}
)


def publish_answer_and_finish_job(
    conn: sqlite3.Connection,
    *,
    answer_id: str,
    question_id: str,
    analysis_id: str,
    asset_id: str,
    job_id: str,
    answer_status: str,
    job_status: str,
    payload_json: str,
    trace_id: str,
    finished_at: str,
) -> None:
    """Atomically publish validated answer and transition QA job to a terminal status."""
    if job_status not in _TERMINAL_QA_JOB_STATUSES:
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            (
                f"cannot publish answer with non-terminal QA job_status '{job_status}'; "
                f"expected one of {sorted(_TERMINAL_QA_JOB_STATUSES)}"
            ),
        )
    try:
        with immediate_transaction(conn):
            conn.execute(
                """
                INSERT INTO answers (
                    answer_id, question_id, analysis_id, asset_id,
                    status, payload, trace_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    answer_id,
                    question_id,
                    analysis_id,
                    asset_id,
                    answer_status,
                    payload_json,
                    trace_id,
                    finished_at,
                ),
            )
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?
                WHERE job_id = ?
                  AND question_id = ?
                  AND analysis_id = ?
                  AND asset_id = ?
                  AND kind = 'qa'
                  AND status IN ('queued', 'running')
                """,
                (job_status, finished_at, job_id, question_id, analysis_id, asset_id),
            )
            if cur.rowcount != 1:
                raise StorageError(
                    ErrorCode.STORAGE_ERROR,
                    f"matching active QA job '{job_id}' not found for answer publication",
                )
    except sqlite3.Error as exc:
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            f"failed to publish answer atomically: {exc}",
        ) from exc
