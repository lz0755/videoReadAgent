"""User-facing operations on the local SQLite queue and committed evidence."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from video_agent.adapters.storage.connection import (
    connect_sqlite,
    create_analysis_with_job,
    create_question_with_job,
    immediate_transaction,
)
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.qa import resolve_evidence
from video_agent.application.jobs import AnalysisRequest, ExecutionMode, QuestionRequest
from video_agent.application.media_timeline import build_playback_map
from video_agent.application.prompt_registry import canonical_json_sha256
from video_agent.application.sampling import SceneProfile
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.models import Answer, FrameEvidence, UsageLedger, VideoAsset


class ResourceMissing(LookupError):
    """The requested resource is unavailable in the supplied scope."""


class SQLiteJobs:
    def __init__(
        self,
        root: Path,
        identities: dict[tuple[SceneProfile, ExecutionMode], str],
        bundle: str,
        ledger: Callable[[str], UsageLedger],
    ) -> None:
        self.db = root / "agent.sqlite3"
        self.files = CatalogArtifactStore(root)
        self.identities = identities
        self.bundle = bundle
        self.ledger = ledger

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with closing(connect_sqlite(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def submit_analysis(
        self, asset: str, request: AnalysisRequest, key: str | None
    ) -> dict[str, Any]:
        with self.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM assets WHERE asset_id=? AND status='ready'", (asset,)
            ).fetchone():
                raise ResourceMissing
            analysis, job = create_analysis_with_job(
                conn,
                analysis_id="an_" + uuid4().hex,
                asset_id=asset,
                job_id="job_" + uuid4().hex,
                config_hash=self.identities[request.profile, request.execution_mode],
                prompt_bundle_hash=self.bundle,
                created_at=datetime.now(UTC).isoformat(),
                idempotency_key=key,
                idempotency_payload_hash=canonical_json_sha256(request.model_dump())
                if key
                else None,
            )
        return {
            "schema_version": "1.1",
            "asset_id": asset,
            "analysis_id": analysis,
            "job_id": job,
            "status": self.job(job)["status"],
        }

    def submit_question(
        self, analysis: str, request: QuestionRequest, key: str | None
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT asset_id,index_state FROM analysis_runs WHERE analysis_id=?", (analysis,)
            ).fetchone()
            if row is None:
                raise ResourceMissing
            if row["index_state"] not in ("ready", "partial"):
                raise StorageError(ErrorCode.INDEX_NOT_READY, "published index required")
            question, job = create_question_with_job(
                conn,
                question_id="q_" + uuid4().hex,
                asset_id=row["asset_id"],
                analysis_id=analysis,
                job_id="job_" + uuid4().hex,
                query_text=request.query_text,
                mode=request.mode,
                created_at=datetime.now(UTC).isoformat(),
                idempotency_key=key,
                idempotency_payload_hash=canonical_json_sha256(request.model_dump())
                if key
                else None,
            )
        return {
            "schema_version": "1.1",
            "asset_id": row["asset_id"],
            "analysis_id": analysis,
            "question_id": question,
            "qa_job_id": job,
            "status": self.job(job)["status"],
        }

    def job(self, job: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT job_id,kind,asset_id,analysis_id,question_id,status,"
                "cancel_requested,attempt_no,"
                "started_at_utc,deadline_at_utc,error_code FROM jobs WHERE job_id=?",
                (job,),
            ).fetchone()
            if row is None:
                raise ResourceMissing
            stages = {
                s: n
                for s, n in conn.execute(
                    "SELECT stage,count(*) FROM stage_results WHERE analysis_id=? "
                    "AND status='committed' GROUP BY stage",
                    (row["analysis_id"],),
                )
            }
            attempts = conn.execute(
                "SELECT count(*) FROM usage_records WHERE job_id=?", (job,)
            ).fetchone()[0]
        return {
            **dict(row),
            "cancel_requested": bool(row["cancel_requested"]),
            "progress": {"committed_stages": stages, "model_attempts": attempts},
        }

    def cancel(self, job: str) -> dict[str, Any]:
        with self.connection() as conn, immediate_transaction(conn):
            conn.execute(
                "UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=? "
                "AND status IN ('queued','running')",
                (datetime.now(UTC).isoformat(), job),
            )
        return self.job(job)

    def analysis(
        self,
        analysis: str,
        offset: int = 0,
        limit: int = 50,
        start_ms: int = 0,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT analysis_id,asset_id,status,index_state,config_hash,prompt_bundle_hash "
                "FROM analysis_runs WHERE analysis_id=?",
                (analysis,),
            ).fetchone()
            if row is None:
                raise ResourceMissing
            rows = conn.execute(
                "SELECT observation_id,kind,text,start_ms,end_ms,provenance FROM observations "
                "WHERE analysis_id=? AND asset_id=? AND end_ms>? AND (? IS NULL OR start_ms<?) "
                "ORDER BY start_ms,end_ms,observation_id LIMIT ? OFFSET ?",
                (analysis, row["asset_id"], start_ms, end_ms, end_ms, limit + 1, offset),
            ).fetchall()
            entries = [
                {
                    **dict(item),
                    "provenance": json.loads(item["provenance"]),
                    "evidence_ids": [
                        r[0]
                        for r in conn.execute(
                            "SELECT evidence_id FROM observation_evidence "
                            "WHERE observation_id=? AND asset_id=? ORDER BY evidence_id",
                            (item["observation_id"], row["asset_id"]),
                        )
                    ],
                }
                for item in rows[:limit]
            ]
        return {
            **dict(row),
            "timeline": entries,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def question(self, question: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT j.status,j.analysis_id,j.asset_id,a.payload "
                "FROM jobs j LEFT JOIN answers a "
                "USING(question_id,analysis_id,asset_id) WHERE j.kind='qa' AND j.question_id=?",
                (question,),
            ).fetchone()
            if row is None:
                raise ResourceMissing
            result: dict[str, Any] = {"status": row["status"]}
            if row["status"] in ("succeeded", "partial") and row["payload"]:
                answer = Answer.model_validate_json(row["payload"])
                if (answer.question_id, answer.analysis_id, answer.asset_id) != (
                    question,
                    row["analysis_id"],
                    row["asset_id"],
                ):
                    raise ResourceMissing
                result.update(answer=answer.model_dump(), usage=self.ledger(question).model_dump())
        return result

    def evidence(
        self,
        analysis: str,
        evidence_id: str,
        representation: Literal["meta", "frame", "source_video"],
    ) -> dict[str, Any] | Path:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT a.* FROM assets a JOIN analysis_runs r USING(asset_id) "
                "WHERE r.analysis_id=? AND a.status='ready'",
                (analysis,),
            ).fetchone()
        if row is None:
            raise ResourceMissing
        asset = VideoAsset.model_validate(
            {
                **{k: row[k] for k in row.keys() if k in VideoAsset.model_fields},
                "has_audio": bool(row["has_audio"]),
            }
        )
        evidence = resolve_evidence(self.db, self.files, asset.asset_id, analysis, evidence_id)
        if evidence is None:
            raise ResourceMissing
        if representation == "meta":
            return {
                "asset_id": asset.asset_id,
                "analysis_id": analysis,
                "kind": "frame" if isinstance(evidence, FrameEvidence) else "transcript",
                "evidence": evidence.model_dump(),
                "playback_timeline_map": build_playback_map(asset).model_dump(),
            }
        try:
            if representation == "frame" and isinstance(evidence, FrameEvidence):
                return self.files.resolve_path(evidence.artifact_id)
            if (
                representation == "source_video"
                and asset.source_artifact_id
                and self.files.verify_hash(asset.source_artifact_id, asset.sha256)
            ):
                return self.files.resolve_path(asset.source_artifact_id)
        except (StorageError, OSError) as exc:
            raise ResourceMissing from exc
        raise ResourceMissing
