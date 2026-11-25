"""Offline SQLite schema bootstrap and sequential migration runner (M1.4).

Requires explicit confirmation that all write entries (API/UI/Worker/CLI) are stopped,
acquires data_root/worker.lock, verifies historical checksums, and executes each SQL
migration inside an explicit BEGIN IMMEDIATE transaction (never sqlite3.executescript).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.domain.errors import ErrorCode, StorageError

_STORAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_SCHEMA_PATH = _STORAGE_DIR / "schema.sql"
_DEFAULT_MIGRATIONS_DIR = _STORAGE_DIR / "migrations"
_MIGRATION_FILE_RE = re.compile(r"^(\d{3})_([a-zA-Z0-9_]+)\.sql$")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_sql_statements(sql_text: str) -> list[str]:
    """Split a SQL script into individual statements using sqlite3.complete_statement."""
    statements: list[str] = []
    buffer_lines: list[str] = []
    for line in sql_text.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer_lines and (not stripped or stripped.startswith("--")):
            continue
        buffer_lines.append(line)
        candidate = "".join(buffer_lines)
        if sqlite3.complete_statement(candidate):
            cleaned = candidate.strip()
            if cleaned:
                statements.append(cleaned)
            buffer_lines = []
    leftover = "".join(buffer_lines).strip()
    if leftover:
        statements.append(leftover)
    return statements


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path, str]]:
    if not migrations_dir.is_dir():
        raise StorageError(
            ErrorCode.MIGRATION_ERROR,
            f"migrations directory not found: {migrations_dir.name}",
        )
    discovered: list[tuple[int, str, Path, str]] = []
    seen_versions: set[int] = set()
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = _MIGRATION_FILE_RE.match(path.name)
        if match is None:
            raise StorageError(
                ErrorCode.MIGRATION_ERROR,
                f"invalid migration filename: {path.name}",
            )
        version = int(match.group(1))
        name = match.group(2)
        if version in seen_versions:
            raise StorageError(
                ErrorCode.MIGRATION_ERROR,
                f"duplicate migration version: {version}",
            )
        seen_versions.add(version)
        checksum = _sha256_bytes(path.read_bytes())
        discovered.append((version, name, path, checksum))
    discovered.sort(key=lambda item: item[0])
    return discovered


def _has_migrations_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def _execute_statements_in_tx(conn: sqlite3.Connection, sql_text: str) -> None:
    statements = _split_sql_statements(sql_text)
    for stmt in statements:
        conn.execute(stmt)


def run_migrations(
    db_path: Path,
    *,
    data_root: Path,
    writers_stopped: bool,
    migrations_dir: Path | None = None,
) -> list[int]:
    """Acquire data_root lock, verify checksums, and apply pending SQL migrations atomically."""
    if not writers_stopped:
        raise StorageError(
            ErrorCode.MIGRATION_ERROR,
            "refusing migration: all write entries (API/UI/Worker/CLI) must be stopped first",
        )

    mig_dir = migrations_dir if migrations_dir is not None else _DEFAULT_MIGRATIONS_DIR
    migrations = _discover_migrations(mig_dir)

    with WorkerLock(data_root):
        conn = connect_sqlite(db_path, bootstrap=True)
        try:
            applied_map: dict[int, tuple[str, str]] = {}
            if _has_migrations_table(conn):
                rows = conn.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                for row in rows:
                    applied_map[int(row[0])] = (str(row[1]), str(row[2]))

            for version, name, _path, checksum in migrations:
                if version in applied_map:
                    recorded_name, recorded_checksum = applied_map[version]
                    if recorded_checksum != checksum or recorded_name != name:
                        raise StorageError(
                            ErrorCode.MIGRATION_ERROR,
                            f"migration {version:03d}_{name} checksum mismatch with history",
                        )

            newly_applied: list[int] = []
            for version, name, path, checksum in migrations:
                if version in applied_map:
                    continue
                sql_text = path.read_text(encoding="utf-8")
                applied_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    with immediate_transaction(conn):
                        _execute_statements_in_tx(conn, sql_text)
                        conn.execute(
                            """
                            INSERT INTO schema_migrations (version, name, checksum, applied_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (version, name, checksum, applied_at),
                        )
                except sqlite3.Error as exc:
                    raise StorageError(
                        ErrorCode.MIGRATION_ERROR,
                        f"migration {version:03d}_{name} failed and rolled back: {exc}",
                    ) from exc
                newly_applied.append(version)

            return newly_applied
        finally:
            conn.close()


def bootstrap_from_schema(
    db_path: Path,
    *,
    data_root: Path,
    writers_stopped: bool,
    schema_path: Path | None = None,
    migrations_dir: Path | None = None,
) -> None:
    """Bootstrap a fresh database from schema.sql and record baseline migration metadata."""
    if not writers_stopped:
        raise StorageError(
            ErrorCode.MIGRATION_ERROR,
            "refusing bootstrap: all write entries (API/UI/Worker/CLI) must be stopped first",
        )

    target_schema = schema_path if schema_path is not None else _DEFAULT_SCHEMA_PATH
    mig_dir = migrations_dir if migrations_dir is not None else _DEFAULT_MIGRATIONS_DIR
    if not target_schema.is_file():
        raise StorageError(ErrorCode.MIGRATION_ERROR, "schema.sql snapshot not found")

    migrations = _discover_migrations(mig_dir)
    schema_sql = target_schema.read_text(encoding="utf-8")

    with WorkerLock(data_root):
        conn = connect_sqlite(db_path, bootstrap=True)
        try:
            if _has_migrations_table(conn):
                raise StorageError(
                    ErrorCode.MIGRATION_ERROR,
                    "cannot bootstrap an already initialized database; use run_migrations",
                )
            applied_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                with immediate_transaction(conn):
                    _execute_statements_in_tx(conn, schema_sql)
                    for version, name, _path, checksum in migrations:
                        conn.execute(
                            """
                            INSERT INTO schema_migrations (version, name, checksum, applied_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (version, name, checksum, applied_at),
                        )
            except sqlite3.Error as exc:
                raise StorageError(
                    ErrorCode.MIGRATION_ERROR,
                    f"schema bootstrap failed and rolled back: {exc}",
                ) from exc
        finally:
            conn.close()


def introspect_schema_structure(conn: sqlite3.Connection) -> dict[str, object]:
    """Return normalized tables, columns, FKs, and indexes for schema equivalence checks."""
    table_rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name NOT LIKE 'search_fts_%'
        ORDER BY name
        """
    ).fetchall()

    tables: dict[str, dict[str, object]] = {}
    for table_name, table_sql in table_rows:
        tname = str(table_name)
        cols = [
            {
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "dflt_value": row[4],
                "pk": int(row[5]),
            }
            for row in conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
        ]
        fks = sorted(
            (
                {
                    "table": str(row[2]),
                    "from": str(row[3]),
                    "to": str(row[4]),
                    "on_update": str(row[5]),
                    "on_delete": str(row[6]),
                }
                for row in conn.execute(f'PRAGMA foreign_key_list("{tname}")').fetchall()
            ),
            key=lambda item: (item["table"], item["from"], item["to"]),
        )
        tables[tname] = {
            "columns": cols,
            "foreign_keys": fks,
            "is_virtual_fts5": "USING fts5" in str(table_sql or ""),
        }

    index_rows = conn.execute(
        """
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND name NOT LIKE 'sqlite_autoindex_%'
        ORDER BY name
        """
    ).fetchall()
    indexes = [
        {"name": str(row[0]), "table": str(row[1]), "sql": str(row[2] or "")} for row in index_rows
    ]

    return {"tables": tables, "indexes": indexes}
