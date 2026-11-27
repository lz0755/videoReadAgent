"""Durable reads and job publication for a single bound question."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.connection import (
    connect_sqlite,
    create_question_with_job,
    immediate_transaction,
)
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.models import (
    Answer,
    FrameEvidence,
    Observation,
    Question,
    RegionId,
    TimeRangeMs,
    TranscriptSegment,
)
from video_agent.domain.ports import SearchHit


def resolve_evidence(
    db: Path, files: CatalogArtifactStore, asset_id: str, analysis_id: str, evidence_id: str
) -> FrameEvidence | TranscriptSegment | None:
    with closing(connect_sqlite(db)) as conn:
        row = conn.execute(
            "SELECT e.kind,e.payload,e.point_ms,e.start_ms,e.end_ms,e.artifact_id,"
            "a.kind,a.sha256 FROM evidence e LEFT JOIN artifacts a "
            "ON (a.asset_id,a.artifact_id)=(e.asset_id,e.artifact_id) "
            "WHERE e.asset_id=? AND e.evidence_id=? AND ("
            "EXISTS (SELECT 1 FROM observation_evidence l JOIN observations o "
            "USING(asset_id,observation_id) WHERE l.asset_id=e.asset_id "
            "AND l.evidence_id=e.evidence_id AND o.analysis_id=?) OR "
            "(e.kind='transcript' AND EXISTS (SELECT 1 FROM search_docs d "
            "WHERE d.asset_id=e.asset_id AND d.analysis_id=? AND d.source_kind='transcript' "
            "AND d.source_id=e.evidence_id)))",
            (
                asset_id,
                evidence_id,
                analysis_id,
                analysis_id,
            ),
        ).fetchone()
    if row is None:
        return None
    data = json.loads(row[1])
    if row[0] == "frame":
        # M3 records a crop's immutable parent alongside the domain payload.
        data.pop("parent_evidence_id", None)
        frame = FrameEvidence.model_validate(data)
        consistent = frame.actual_pts_ms == row[2] and frame.artifact_id == row[5]
        if row[6] != ("crop" if frame.crop else "frame") or not row[7]:
            return None
        try:
            if not files.verify_hash(row[5], row[7]):
                return None
        except StorageError, OSError:
            return None
        item: FrameEvidence | TranscriptSegment = frame
    else:
        item = TranscriptSegment.model_validate(data)
        consistent = (item.time_range.start_ms, item.time_range.end_ms) == (row[3], row[4])
    if not consistent or (item.asset_id, item.evidence_id) != (
        asset_id,
        evidence_id,
    ):
        raise StorageError(ErrorCode.INVALID_CITATION, "stored evidence identity differs")
    return item


class SQLiteQA:
    def __init__(self, db: Path, question: Question) -> None:
        self.db = db
        self.question = question
        self._files = CatalogArtifactStore(db.parent)

    @property
    def scope(self) -> tuple[str, str]:
        return self.question.asset_id, self.question.analysis_id

    def begin(self, job_id: str) -> None:
        q = self.question
        with closing(connect_sqlite(self.db)) as conn:
            if not conn.execute(
                "SELECT 1 FROM analysis_runs WHERE asset_id=? AND analysis_id=?", self.scope
            ).fetchone():
                raise StorageError(ErrorCode.INDEX_NOT_READY, "bound analysis is unavailable")
            create_question_with_job(
                conn,
                question_id=q.question_id,
                asset_id=q.asset_id,
                analysis_id=q.analysis_id,
                job_id=job_id,
                query_text=q.query_text,
                mode=q.mode,
                created_at=q.created_at_utc,
            )
            conn.execute(
                "UPDATE jobs SET status='running' WHERE job_id=? AND cancel_requested=0 "
                "AND status='queued'",
                (job_id,),
            )

    def resolve(self, evidence_id: str) -> FrameEvidence | TranscriptSegment | None:
        return resolve_evidence(self.db, self._files, *self.scope, evidence_id)

    def read_observation(self, hit: SearchHit) -> Observation:
        invalid = StorageError(ErrorCode.INVALID_CITATION, "unbound or changed index document")
        if (hit.asset_id, hit.analysis_id) != self.scope:
            raise invalid
        with closing(connect_sqlite(self.db)) as conn:
            doc = conn.execute(
                "SELECT source_kind,source_id,original_text,start_ms,end_ms FROM search_docs "
                "WHERE asset_id=? AND analysis_id=? AND doc_id=?",
                (*self.scope, hit.doc_id),
            ).fetchone()
            if doc != (
                hit.source_kind,
                hit.source_id,
                hit.text,
                hit.time_range.start_ms,
                hit.time_range.end_ms,
            ):
                raise invalid
            if hit.source_kind == "transcript":
                source = self.resolve(hit.source_id)
                if (
                    not isinstance(source, TranscriptSegment)
                    or source.text != hit.text
                    or source.time_range != hit.time_range
                    or hit.evidence_ids != [hit.source_id]
                ):
                    raise invalid
                return Observation(
                    observation_id="transcript_" + hit.doc_id,
                    asset_id=hit.asset_id,
                    analysis_id=hit.analysis_id,
                    kind="heard",
                    text=source.text,
                    time_range=source.time_range,
                    evidence_ids=[source.evidence_id],
                    provenance={"asr_version": source.asr_version},
                )
            row = conn.execute(
                "SELECT kind,text,start_ms,end_ms,region_hint,provenance FROM observations "
                "WHERE asset_id=? AND analysis_id=? AND observation_id=?",
                (*self.scope, hit.source_id),
            ).fetchone()
            refs = [
                r[0]
                for r in conn.execute(
                    "SELECT l.evidence_id FROM observation_evidence l JOIN evidence "
                    "USING(evidence_id,asset_id) WHERE l.asset_id=? AND l.observation_id=? "
                    "ORDER BY l.evidence_id",
                    (hit.asset_id, hit.source_id),
                )
            ]
            if not row or row[1:4] != doc[2:] or refs != sorted(hit.evidence_ids):
                raise invalid
            return Observation(
                observation_id=hit.source_id,
                asset_id=hit.asset_id,
                analysis_id=hit.analysis_id,
                kind=row[0],
                text=row[1],
                time_range=hit.time_range,
                region_hint=row[4],
                evidence_ids=refs,
                provenance=json.loads(row[5]),
            )

    def allow_region(self, region: RegionId, window: TimeRangeMs) -> bool:
        if region == "full":
            return True
        with closing(connect_sqlite(self.db)) as conn:
            row = conn.execute(
                "SELECT 1 FROM observations o JOIN observation_evidence l "
                "USING(observation_id,asset_id) JOIN evidence e USING(evidence_id,asset_id) "
                "JOIN evidence p ON p.asset_id=e.asset_id AND p.kind='frame' "
                "AND p.evidence_id=json_extract(o.provenance,'$.parent_frame') "
                "AND p.evidence_id=coalesce(json_extract(e.payload,'$.parent_evidence_id'),"
                "e.evidence_id) WHERE o.asset_id=? AND o.analysis_id=? AND o.region_hint=? "
                "AND e.kind='frame' AND json_extract(json_extract(o.provenance,'$.hint_window'),"
                "'$.start_ms')<=? AND json_extract(json_extract(o.provenance,'$.hint_window'),"
                "'$.end_ms')>=? LIMIT 1",
                (*self.scope, region, window.start_ms, window.end_ms),
            ).fetchone()
        return row is not None

    def context(self, hit: SearchHit) -> list[FrameEvidence]:
        if (hit.asset_id, hit.analysis_id) != self.scope or hit.source_kind != "observation":
            return []
        observation = self.read_observation(hit)
        if observation.kind != "observed":
            return []
        with closing(connect_sqlite(self.db)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT e.evidence_id,e.point_ms FROM search_docs d JOIN observations o "
                "ON (d.asset_id,d.analysis_id,d.source_id)="
                "(o.asset_id,o.analysis_id,o.observation_id) "
                "JOIN observation_evidence l ON l.asset_id=o.asset_id "
                "AND l.observation_id=o.observation_id JOIN evidence e "
                "ON (e.evidence_id,e.asset_id)=(l.evidence_id,l.asset_id) "
                "WHERE d.asset_id=? AND d.analysis_id=? AND d.source_kind='observation' "
                "AND o.kind='observed' AND e.kind='frame' ORDER BY e.point_ms,e.evidence_id",
                self.scope,
            ).fetchall()
        cited = set(observation.evidence_ids)
        inside = [r for r in rows if r[0] in cited]
        if not inside:
            return []
        before = [r for r in rows if r[1] < inside[0][1]]
        after = [r for r in rows if r[1] > inside[-1][1]]
        return [
            point
            for eid, _ms in before[-1:] + inside + after[:1]
            if isinstance(point := self.resolve(eid), FrameEvidence)
        ]

    def fail(self, job_id: str, code: ErrorCode) -> ErrorCode:
        with closing(connect_sqlite(self.db)) as conn, immediate_transaction(conn):
            row = self._active_job(conn, job_id)
            if row and (row[0] or row[1] == "cancelled"):
                code = ErrorCode.JOB_CANCELLED
            conn.execute(
                "UPDATE jobs SET status=?,error_code=?,updated_at=? WHERE job_id=? "
                "AND question_id=? AND status IN ('queued','running','cancelled')",
                (
                    "cancelled" if code == ErrorCode.JOB_CANCELLED else "failed",
                    code.value,
                    datetime.now(UTC).isoformat(),
                    job_id,
                    self.question.question_id,
                ),
            )
        return code

    def _active_job(self, conn: sqlite3.Connection, job_id: str) -> tuple[int, str] | None:
        row: tuple[int, str] | None = conn.execute(
            "SELECT cancel_requested,status FROM jobs WHERE asset_id=? AND analysis_id=? "
            "AND question_id=? AND job_id=? AND kind='qa'",
            (*self.scope, self.question.question_id, job_id),
        ).fetchone()
        return row

    def publish(self, answer: Answer, job_id: str) -> None:
        if (answer.asset_id, answer.analysis_id, answer.question_id) != (
            *self.scope,
            self.question.question_id,
        ):
            raise StorageError(ErrorCode.INVALID_CITATION, "answer belongs to another question")
        with closing(connect_sqlite(self.db)) as conn, immediate_transaction(conn):
            row = self._active_job(conn, job_id)
            if row and (row[0] or row[1] == "cancelled"):
                raise StorageError(ErrorCode.JOB_CANCELLED, "question was cancelled")
            if not row or row[1] not in ("queued", "running"):
                raise StorageError(ErrorCode.STORAGE_ERROR, "active bound QA job required")
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO answers(answer_id,question_id,analysis_id,asset_id,status,payload,"
                "trace_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    answer.answer_id,
                    answer.question_id,
                    answer.analysis_id,
                    answer.asset_id,
                    answer.status,
                    answer.model_dump_json(),
                    answer.trace_id,
                    now,
                ),
            )
            conn.execute(
                "UPDATE jobs SET status=?,updated_at=? WHERE job_id=?",
                ("partial" if answer.status == "partial" else "succeeded", now, job_id),
            )
