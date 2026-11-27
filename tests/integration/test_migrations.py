"""Real disk SQLite migration, rollback, schema equivalence, and OS worker lock tests (M1.4)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.migrate import (
    bootstrap_from_schema,
    introspect_schema_structure,
    run_migrations,
)
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.domain.errors import ErrorCode, StorageError, WorkerLockError

REQUIRED_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "assets",
        "artifacts",
        "analysis_runs",
        "questions",
        "jobs",
        "evidence",
        "observations",
        "observation_evidence",
        "answers",
        "search_docs",
        "search_fts",
        "budget_accounts",
        "usage_records",
        "stage_results",
    }
)


def test_schema_bootstrap_and_sequential_migrations_are_structurally_equivalent(
    tmp_path: Path,
) -> None:
    """Fresh bootstrap from schema.sql matches incremental upgrade from migrations/*.sql."""
    bootstrap_dir = tmp_path / "from_schema"
    migrate_dir = tmp_path / "from_migrations"
    bootstrap_dir.mkdir()
    migrate_dir.mkdir()

    db_schema = bootstrap_dir / "agent.db"
    db_migrated = migrate_dir / "agent.db"

    bootstrap_from_schema(db_schema, data_root=bootstrap_dir, writers_stopped=True)
    applied = run_migrations(db_migrated, data_root=migrate_dir, writers_stopped=True)
    assert applied == [1, 2]

    # Re-running migrations is a no-op
    assert run_migrations(db_migrated, data_root=migrate_dir, writers_stopped=True) == []

    c1 = connect_sqlite(db_schema)
    c2 = connect_sqlite(db_migrated)
    try:
        s1 = introspect_schema_structure(c1)
        s2 = introspect_schema_structure(c2)
        tables_obj = s1["tables"]
        assert isinstance(tables_obj, dict)
        assert REQUIRED_TABLES.issubset(set(tables_obj.keys()))
        assert s1 == s2
    finally:
        c1.close()
        c2.close()


def test_migration_requires_writers_stopped_and_exclusive_os_lock(tmp_path: Path) -> None:
    """Migration refuses when writers_stopped=False or when WorkerLock is held elsewhere."""
    db_path = tmp_path / "locked.db"

    with pytest.raises(StorageError) as exc_writers:
        run_migrations(db_path, data_root=tmp_path, writers_stopped=False)
    assert exc_writers.value.code is ErrorCode.MIGRATION_ERROR

    # Real separate subprocess holds the OS lock while parent attempts migration & lock acquisition
    holder_code = (
        "import sys, time; from pathlib import Path; "
        "from video_agent.adapters.storage.worker_lock import WorkerLock; "
        "lock = WorkerLock(Path(sys.argv[1])); "
        "lock.acquire(); "
        "print('LOCKED', flush=True); "
        "sys.stdin.readline(); "
        "lock.release()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready_line = proc.stdout.readline().strip()
        assert ready_line == "LOCKED", (
            f"lock holder failed: {proc.stderr.read() if proc.stderr else ''}"
        )

        with pytest.raises(WorkerLockError) as exc_lock:
            WorkerLock(tmp_path).acquire()
        assert exc_lock.value.code is ErrorCode.WORKER_ALREADY_RUNNING

        with pytest.raises(WorkerLockError) as exc_mig_lock:
            run_migrations(db_path, data_root=tmp_path, writers_stopped=True)
        assert exc_mig_lock.value.code is ErrorCode.WORKER_ALREADY_RUNNING
    finally:
        if proc.stdin is not None:
            proc.stdin.write("release\n")
            proc.stdin.flush()
        proc.wait(timeout=5)

    # Lock file must NOT be unlinked after normal release; subsequent migration succeeds
    assert (tmp_path / "worker.lock").exists()
    applied = run_migrations(db_path, data_root=tmp_path, writers_stopped=True)
    assert applied == [1, 2]


def test_migration_rollback_on_mid_script_failure_and_checksum_tamper_detection(
    tmp_path: Path,
) -> None:
    """Mid-migration failure rolls back DDL + leaves no bogus version; checksum tampering fails."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    src_initial = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "video_agent"
        / "adapters"
        / "storage"
        / "migrations"
        / "001_initial.sql"
    )
    shutil.copy2(src_initial, mig_dir / "001_initial.sql")

    db_path = tmp_path / "rollback.db"
    assert run_migrations(
        db_path,
        data_root=tmp_path,
        writers_stopped=True,
        migrations_dir=mig_dir,
    ) == [1]

    # Add broken multi-statement migration 002: statement 1 creates a table, statement 2 fails
    broken_sql = mig_dir / "002_broken.sql"
    broken_sql.write_text(
        "CREATE TABLE should_roll_back (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table_boom (id) VALUES (1);\n",
        encoding="utf-8",
    )

    with pytest.raises(StorageError) as exc_fail:
        run_migrations(
            db_path,
            data_root=tmp_path,
            writers_stopped=True,
            migrations_dir=mig_dir,
        )
    assert exc_fail.value.code is ErrorCode.MIGRATION_ERROR

    conn = connect_sqlite(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "should_roll_back" not in tables
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1]
    finally:
        conn.close()

    # Remove broken 002 and tamper with already-applied 001_initial.sql -> checksum mismatch fails
    broken_sql.unlink()
    tampered = mig_dir / "001_initial.sql"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")

    with pytest.raises(StorageError) as exc_tamper:
        run_migrations(
            db_path,
            data_root=tmp_path,
            writers_stopped=True,
            migrations_dir=mig_dir,
        )
    assert exc_tamper.value.code is ErrorCode.MIGRATION_ERROR
