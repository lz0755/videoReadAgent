"""Transactional observation, evidence and provenance storage."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.application.observations import WindowResult
from video_agent.application.vision_validation import validate_window
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.models import FrameEvidence, TranscriptSegment


class SQLiteObservations:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def save(self, result: WindowResult) -> None:
        with closing(connect_sqlite(self._path)) as conn, immediate_transaction(conn):
            write_window(conn, result)


def _write_evidence(
    conn: sqlite3.Connection, item: FrameEvidence | TranscriptSegment, now: str
) -> None:
    old = conn.execute(
        "SELECT payload,asset_id FROM evidence WHERE evidence_id=?", (item.evidence_id,)
    ).fetchone()
    if old:
        if old[1] != item.asset_id or json.loads(old[0]) != item.model_dump():
            raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "immutable evidence differs")
        return
    if isinstance(item, FrameEvidence):
        kind = "frame"
        point, start, end, artifact = item.actual_pts_ms, None, None, item.artifact_id
        row = conn.execute(
            "SELECT kind FROM artifacts WHERE artifact_id=? AND asset_id=?",
            (artifact, item.asset_id),
        ).fetchone()
        if not row or row[0] not in ("frame", "crop"):
            raise StorageError(ErrorCode.INVALID_CITATION, "frame artifact owner or kind differs")
    else:
        kind = "transcript"
        point, start, end, artifact = None, item.time_range.start_ms, item.time_range.end_ms, None
    conn.execute(
        "INSERT INTO evidence(evidence_id,asset_id,kind,point_ms,start_ms,end_ms,"
        "artifact_id,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            item.evidence_id,
            item.asset_id,
            kind,
            point,
            start,
            end,
            artifact,
            item.model_dump_json(),
            now,
        ),
    )


def write_window(conn: sqlite3.Connection, result: WindowResult) -> None:
    inputs = result.inputs
    validate_window(inputs, result.observations)
    if not conn.in_transaction:
        raise StorageError(ErrorCode.STORAGE_ERROR, "window writes require a transaction")
    if not conn.execute(
        "SELECT 1 FROM analysis_runs WHERE analysis_id=? AND asset_id=?",
        (inputs.analysis_id, inputs.asset_id),
    ).fetchone():
        raise StorageError(ErrorCode.CROSS_ASSET_REFERENCE, "unknown analysis owner")
    now = datetime.now(UTC).isoformat()
    for source in [*result.originals, *inputs.frames, *inputs.transcripts]:
        if source.asset_id != inputs.asset_id:
            raise StorageError(ErrorCode.CROSS_ASSET_REFERENCE, "foreign window evidence")
        _write_evidence(conn, source, now)
    for obs in result.observations:
        row = (
            obs.observation_id,
            obs.analysis_id,
            obs.asset_id,
            obs.kind,
            obs.text,
            obs.time_range.start_ms,
            obs.time_range.end_ms,
            obs.region_hint,
            json.dumps(obs.provenance, sort_keys=True),
        )
        old = conn.execute(
            "SELECT observation_id,analysis_id,asset_id,kind,text,start_ms,end_ms,"
            "region_hint,provenance FROM observations WHERE observation_id=?",
            (obs.observation_id,),
        ).fetchone()
        refs = set(obs.evidence_ids)
        if old:
            links = {
                r[0]
                for r in conn.execute(
                    "SELECT evidence_id FROM observation_evidence WHERE observation_id=?",
                    (obs.observation_id,),
                )
            }
            if tuple(old) != row or links != refs:
                raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "immutable observation differs")
        else:
            conn.execute(
                "INSERT INTO observations(observation_id,analysis_id,asset_id,kind,text,"
                "start_ms,end_ms,region_hint,provenance,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*row, now),
            )
            conn.executemany(
                "INSERT INTO observation_evidence VALUES(?,?,?)",
                [(obs.observation_id, e, obs.asset_id) for e in sorted(refs)],
            )
