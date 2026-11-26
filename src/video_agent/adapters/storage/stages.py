"""Publish windows atomically after writing and verifying their owned artifacts."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.adapters.storage.observations import write_window
from video_agent.application.observations import WindowResult
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.models import StrictModel
from video_agent.domain.ports import ArtifactStore


class _Manifest(StrictModel):
    item: str
    key: str
    result: WindowResult
    artifacts: dict[str, str]


def _frame_ids(result: WindowResult) -> set[str]:
    return {f.artifact_id for f in [*result.inputs.frames, *result.originals]}


def invalidate_index(conn: sqlite3.Connection, asset_id: str, analysis_id: str) -> None:
    conn.execute(
        "UPDATE analysis_runs SET index_state='pending' WHERE analysis_id=? AND asset_id=? "
        "AND index_state IN ('ready','partial')",
        (analysis_id, asset_id),
    )
    conn.execute(
        "UPDATE stage_results SET status='invalidated' WHERE analysis_id=? AND asset_id=? "
        "AND stage='index' AND status='committed'",
        (analysis_id, asset_id),
    )


class StageCache:
    def __init__(self, db_path: Path, artifact_store: ArtifactStore) -> None:
        self._db = db_path
        self._files = artifact_store

    def _verified_hash(
        self, conn: sqlite3.Connection, asset: str, aid: str, *, export: bool = False
    ) -> str:
        row = conn.execute(
            "SELECT kind,sha256 FROM artifacts WHERE asset_id=? AND artifact_id=?", (asset, aid)
        ).fetchone()
        if (
            not row
            or row[0] not in (("export",) if export else ("frame", "crop"))
            or not self._files.verify_hash(aid, row[1])
        ):
            raise StorageError(ErrorCode.STORAGE_ERROR, "stage dependency failed integrity check")
        return str(row[1])

    def load(self, asset_id: str, analysis_id: str, item: str, key: str) -> WindowResult | None:
        owner = (analysis_id, asset_id, item)
        with closing(connect_sqlite(self._db)) as conn:
            row = conn.execute(
                "SELECT config_hash,artifact_id,checksum FROM stage_results WHERE analysis_id=? "
                "AND asset_id=? AND item_key=? AND stage='observe' AND status='committed'",
                owner,
            ).fetchone()
            if row is None:
                return None
            old_key, aid, checksum = row
            if old_key == key:
                try:
                    if self._verified_hash(conn, asset_id, aid, export=True) != checksum:
                        raise ValueError("stage digest changed")
                    raw = self._files.read_bytes(aid)
                    if hashlib.sha256(raw).hexdigest() != checksum:
                        raise ValueError("stage bytes changed during read")
                    manifest = _Manifest.model_validate_json(raw)
                    result = manifest.result
                    if (
                        manifest.item,
                        manifest.key,
                        result.inputs.asset_id,
                        result.inputs.analysis_id,
                    ) != (item, key, asset_id, analysis_id):
                        raise ValueError("stage identity mismatch")
                    if manifest.artifacts != {
                        ref: self._verified_hash(conn, asset_id, ref) for ref in _frame_ids(result)
                    }:
                        raise ValueError("frame digest changed")
                    return result
                except OSError, ValueError, StorageError, ValidationError:
                    pass
            with immediate_transaction(conn):
                changed = conn.execute(
                    "UPDATE stage_results SET status='invalidated' WHERE analysis_id=? "
                    "AND asset_id=? AND item_key=? AND stage='observe' AND checksum=? "
                    "AND config_hash=? AND status='committed'",
                    (*owner, checksum, old_key),
                )
                if changed.rowcount:
                    invalidate_index(conn, asset_id, analysis_id)
            return None

    def commit(self, item: str, key: str, result: WindowResult) -> None:
        asset = result.inputs.asset_id
        analysis = result.inputs.analysis_id
        prior = self.load(asset, analysis, item, key)
        if prior is not None:
            if prior != result:
                raise StorageError(
                    ErrorCode.IDEMPOTENCY_CONFLICT, "different result for committed window"
                )
            return
        with closing(connect_sqlite(self._db)) as conn:
            hashes = {aid: self._verified_hash(conn, asset, aid) for aid in _frame_ids(result)}
            raw = (
                _Manifest(item=item, key=key, result=result, artifacts=hashes)
                .model_dump_json()
                .encode()
            )
            digest = hashlib.sha256(raw).hexdigest()
            aid = self._files.put_bytes(asset, "export", raw, suffix=".json")
            with immediate_transaction(conn):
                if self._verified_hash(conn, asset, aid, export=True) != digest:
                    raise StorageError(
                        ErrorCode.STORAGE_ERROR, "stage artifact failed verification"
                    )
                old = conn.execute(
                    "SELECT checksum FROM stage_results WHERE analysis_id=? AND asset_id=? "
                    "AND stage='observe' AND item_key=? AND config_hash=? AND status='committed'",
                    (analysis, asset, item, key),
                ).fetchone()
                if old and old[0] != digest:
                    raise StorageError(
                        ErrorCode.IDEMPOTENCY_CONFLICT, "window committed concurrently"
                    )
                write_window(conn, result)
                conn.execute(
                    """INSERT INTO stage_results(analysis_id,asset_id,stage,item_key,config_hash,
                             artifact_id,checksum,status,created_at) VALUES(?,?,'observe',?,?,?,?,'committed',?)
                             ON CONFLICT(analysis_id,stage,item_key) DO UPDATE SET config_hash=excluded.config_hash,
                             artifact_id=excluded.artifact_id,checksum=excluded.checksum,status='committed',
                             created_at=excluded.created_at""",
                    (analysis, asset, item, key, aid, digest, datetime.now(UTC).isoformat()),
                )
                invalidate_index(conn, asset, analysis)
