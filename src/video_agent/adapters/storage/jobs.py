"""User-facing operations on the local SQLite queue and committed evidence."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
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
from video_agent.adapters.storage.stages import StageCache, invalidate_index
from video_agent.application.jobs import AnalysisRequest, ExecutionMode, QuestionRequest
from video_agent.application.media_timeline import build_playback_map
from video_agent.application.prompt_registry import canonical_json_sha256
from video_agent.application.sampling import SceneProfile
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.models import Answer, FrameEvidence, Job, Question, UsageLedger, VideoAsset


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

    def _unpublished_operations(self) -> set[str]:
        uncertain = set()
        cache = StageCache(self.db, self.files)
        with self.connection() as conn:
            jobs = conn.execute(
                "SELECT job_id,asset_id,analysis_id FROM jobs WHERE kind='analysis' "
                "AND status='running' AND cancel_requested=0"
            )
            for job, asset, analysis in jobs:
                sent = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT json_extract(reservation_json,'$.operation_key') "
                        "FROM usage_records "
                        "WHERE job_id=? AND status IN ('succeeded','failed')",
                        (job,),
                    )
                }
                if not sent:
                    continue
                committed: set[str] = set()
                windows = conn.execute(
                    "SELECT item_key,config_hash FROM stage_results WHERE analysis_id=? "
                    "AND stage='observe' AND status='committed'",
                    (analysis,),
                ).fetchall()
                for item, key in windows:
                    result = cache.load(asset, analysis, item, key)
                    if result is None:
                        continue
                    batch = json.loads(result.provenance.sampling).get("max_images")
                    if type(batch) is int and batch > 0:
                        prefix = cache.operation_key(analysis, item, key)
                        committed.update(
                            f"{prefix}/{offset}"
                            for offset in range(0, len(result.inputs.frames), batch)
                        )
                if not sent.issubset(committed):
                    uncertain.add(job)
        return uncertain

    def _citations_exist(self, answer: Answer) -> bool:
        references = {ref for claim in answer.claims for ref in claim.evidence_ids}
        if answer.localization:
            for bracket in (answer.localization.start_bracket, answer.localization.end_bracket):
                references.update((bracket.before_evidence_id, bracket.after_evidence_id))
        try:
            return all(
                resolve_evidence(self.db, self.files, answer.asset_id, answer.analysis_id, ref)
                is not None
                for ref in references
            )
        except ValueError, StorageError, OSError:
            return False

    def recover(self) -> None:
        """Called by the owner of worker.lock before its first claim."""
        uncertain = self._unpublished_operations()
        now = datetime.now(UTC)
        with self.connection() as conn, immediate_transaction(conn):
            rows = conn.execute(
                "SELECT job_id,analysis_id,kind,cancel_requested,deadline_at_utc "
                "FROM jobs WHERE status='running' OR (status='queued' AND cancel_requested=1)"
            ).fetchall()
            for row in rows:
                code = None
                if row["cancel_requested"]:
                    code = ErrorCode.JOB_CANCELLED
                elif (
                    row["job_id"] in uncertain
                    or conn.execute(
                        "SELECT 1 FROM usage_records WHERE job_id=? AND "
                        "(status IN ('unknown','pending') OR "
                        "(?='qa' AND status!='not_sent')) LIMIT 1",
                        (row["job_id"], row["kind"]),
                    ).fetchone()
                ):
                    code = ErrorCode.EXTERNAL_RESULT_UNKNOWN
                elif (
                    row["deadline_at_utc"] and datetime.fromisoformat(row["deadline_at_utc"]) <= now
                ):
                    code = ErrorCode.DEADLINE_EXCEEDED
                state = (
                    "queued"
                    if code is None
                    else ("cancelled" if row["cancel_requested"] else "failed")
                )
                conn.execute(
                    "UPDATE jobs SET status=?,error_code=?,updated_at=? WHERE job_id=?",
                    (state, code, now.isoformat(), row["job_id"]),
                )
                if row["kind"] == "analysis":
                    conn.execute(
                        "UPDATE analysis_runs SET status=?, index_state=? WHERE analysis_id=?",
                        (state, "pending" if state == "queued" else "failed", row["analysis_id"]),
                    )
        self._verify_published()

    def _verify_published(self) -> None:
        stages = StageCache(self.db, self.files)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.analysis_id,a.asset_id,a.source_artifact_id,a.sha256 "
                "FROM analysis_runs r JOIN assets a USING(asset_id) "
                "WHERE r.status IN ('succeeded','partial') "
                "AND r.index_state IN ('ready','partial')"
            ).fetchall()
            for analysis, asset, source, digest in rows:
                windows = conn.execute(
                    "SELECT item_key,config_hash FROM stage_results WHERE analysis_id=? "
                    "AND asset_id=? AND stage='observe' AND status='committed'",
                    (analysis, asset),
                ).fetchall()
                summary = conn.execute(
                    "SELECT artifact_id,checksum FROM stage_results WHERE analysis_id=? "
                    "AND asset_id=? AND stage='summarize' AND status='committed'",
                    (analysis, asset),
                ).fetchone()
                try:
                    valid = bool(
                        windows
                        and summary
                        and self.files.verify_hash(source, digest)
                        and self.files.verify_hash(summary[0], summary[1])
                        and all(stages.load(asset, analysis, k, v) is not None for k, v in windows)
                    )
                except OSError, ValueError, StorageError:
                    valid = False
                if not valid:
                    with immediate_transaction(conn):
                        invalidate_index(conn, asset, analysis)
                        conn.execute(
                            "UPDATE analysis_runs SET status='failed',index_state='failed' "
                            "WHERE analysis_id=?",
                            (analysis,),
                        )
                        conn.execute(
                            "UPDATE jobs SET status='failed',error_code=?,updated_at=? "
                            "WHERE kind='analysis' AND analysis_id=?",
                            (ErrorCode.STORAGE_ERROR, datetime.now(UTC).isoformat(), analysis),
                        )

            answers = conn.execute(
                "SELECT j.job_id,a.payload FROM jobs j JOIN answers a "
                "USING(question_id,analysis_id,asset_id) WHERE j.status IN ('succeeded','partial')"
            ).fetchall()
            for job, payload in answers:
                if not self._citations_exist(Answer.model_validate_json(payload)):
                    with immediate_transaction(conn):
                        conn.execute(
                            "UPDATE jobs SET status='failed',error_code=?,updated_at=? "
                            "WHERE job_id=?",
                            (ErrorCode.INVALID_CITATION, datetime.now(UTC).isoformat(), job),
                        )

    def apply_queued_cancellations(self) -> None:
        with self.connection() as conn, immediate_transaction(conn):
            ids = conn.execute(
                "UPDATE jobs SET status='cancelled',error_code=?,updated_at=? "
                "WHERE status='queued' AND cancel_requested=1 RETURNING kind,analysis_id",
                (ErrorCode.JOB_CANCELLED, datetime.now(UTC).isoformat()),
            ).fetchall()
            for kind, analysis in ids:
                if kind == "analysis":
                    conn.execute(
                        "UPDATE analysis_runs SET status='cancelled',index_state='failed' "
                        "WHERE analysis_id=?",
                        (analysis,),
                    )

    def retry(self, job_id: str, *, confirmed: bool) -> dict[str, Any]:
        with self.connection() as conn, immediate_transaction(conn):
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ResourceMissing
            if (
                row["status"] != "failed"
                or row["cancel_requested"]
                or conn.execute(
                    "SELECT 1 FROM answers WHERE question_id=?", (row["question_id"],)
                ).fetchone()
            ):
                raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "job has no retryable failure")
            if row["deadline_at_utc"] and datetime.fromisoformat(
                row["deadline_at_utc"]
            ) <= datetime.now(UTC):
                raise StorageError(ErrorCode.DEADLINE_EXCEEDED, "original deadline elapsed")
            uncertain = row["error_code"] == ErrorCode.EXTERNAL_RESULT_UNKNOWN or bool(
                conn.execute(
                    "SELECT 1 FROM usage_records WHERE job_id=? "
                    "AND status IN ('pending','unknown') LIMIT 1",
                    (job_id,),
                ).fetchone()
            )
            if uncertain and not confirmed:
                raise StorageError(ErrorCode.EXTERNAL_RESULT_UNKNOWN, "confirmation required")
            # Only the explicit command enqueues a job with this error. Claim consumes it once.
            conn.execute(
                "UPDATE jobs SET status='queued',error_code=?,updated_at=? WHERE job_id=?",
                (
                    ErrorCode.EXTERNAL_RESULT_UNKNOWN if uncertain else None,
                    datetime.now(UTC).isoformat(),
                    job_id,
                ),
            )
            if row["kind"] == "analysis":
                conn.execute(
                    "UPDATE analysis_runs SET status='queued',index_state='pending' "
                    "WHERE analysis_id=?",
                    (row["analysis_id"],),
                )
        return self.job(job_id)

    def uses_audio_process(self, job: Job) -> bool:
        with self.connection() as conn:
            return job.kind == "analysis" and bool(
                conn.execute(
                    "SELECT has_audio FROM assets WHERE asset_id=?", (job.asset_id,)
                ).fetchone()[0]
            )

    def check_computation(self, job_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT cancel_requested,deadline_at_utc,status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None or row["cancel_requested"] or row["status"] != "running":
            return True
        if row["deadline_at_utc"] and datetime.fromisoformat(
            row["deadline_at_utc"]
        ) <= datetime.now(UTC):
            raise StorageError(ErrorCode.DEADLINE_EXCEEDED, "computation deadline elapsed")
        return False

    def claim(self, analysis_seconds: float, qa_seconds: float) -> Job | None:
        with self.connection() as conn, immediate_transaction(conn):
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_at,rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = datetime.now(UTC)
            seconds = qa_seconds if row["kind"] == "qa" else analysis_seconds
            data = dict(
                conn.execute(
                    "UPDATE jobs SET status='running',attempt_no=attempt_no+1,"
                    "started_at_utc=coalesce(started_at_utc,?),"
                    "deadline_at_utc=coalesce(deadline_at_utc,?),updated_at=?,error_code=NULL "
                    "WHERE job_id=? AND status='queued' RETURNING *",
                    (
                        now.isoformat(),
                        (now + timedelta(seconds=seconds)).isoformat(),
                        now.isoformat(),
                        row["job_id"],
                    ),
                ).fetchone()
            )
            if row["kind"] == "analysis":
                conn.execute(
                    "UPDATE analysis_runs SET status='running' WHERE analysis_id=?",
                    (row["analysis_id"],),
                )
            data["cancel_requested"] = bool(data["cancel_requested"])
            data["error_code"] = row["error_code"]
            return Job.model_validate({k: v for k, v in data.items() if k in Job.model_fields})

    def fail(self, job: Job, code: ErrorCode) -> None:
        with self.connection() as conn, immediate_transaction(conn):
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=? AND status='running'",
                (job.job_id,),
            ).fetchone()
            if row is None:
                return
            if row[0]:
                code = ErrorCode.JOB_CANCELLED
            status = "cancelled" if code == ErrorCode.JOB_CANCELLED else "failed"
            conn.execute(
                "UPDATE jobs SET status=?,error_code=?,updated_at=? WHERE job_id=?",
                (status, code.value, datetime.now(UTC).isoformat(), job.job_id),
            )
            if job.kind == "analysis":
                conn.execute(
                    "UPDATE analysis_runs SET status=?,index_state='failed' WHERE analysis_id=?",
                    (status, job.analysis_id),
                )

    def parameters(self, job: Job) -> tuple[SceneProfile, ExecutionMode]:
        with self.connection() as conn:
            fingerprint, bundle = conn.execute(
                "SELECT config_hash,prompt_bundle_hash "
                "FROM analysis_runs WHERE analysis_id=? AND asset_id=?",
                (job.analysis_id, job.asset_id),
            ).fetchone()
        for options, value in self.identities.items():
            if fingerprint == value and bundle == self.bundle:
                return options
        raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "queued analysis configuration changed")

    def bound_question(self, job: Job) -> Question:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT question_id,asset_id,analysis_id,query_text,mode,"
                "created_at AS created_at_utc FROM questions WHERE question_id=? AND analysis_id=? "
                "AND asset_id=?",
                (job.question_id, job.analysis_id, job.asset_id),
            ).fetchone()
        if row is None:
            raise ResourceMissing
        return Question.model_validate(dict(row))

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
            "progress": {
                "committed_stages": stages if row["kind"] == "analysis" else {},
                "model_attempts": attempts,
            },
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
            if row["status"] in ("succeeded", "partial"):
                if not row["payload"]:
                    return {"status": "failed"}
                answer = Answer.model_validate_json(row["payload"])
                if (answer.question_id, answer.analysis_id, answer.asset_id) != (
                    question,
                    row["analysis_id"],
                    row["asset_id"],
                ):
                    raise ResourceMissing
                if not self._citations_exist(answer):
                    return {"status": "failed"}
                result.update(answer=answer.model_dump(), usage=self.ledger(question).model_dump())
        return result

    def report(self, analysis: str) -> dict[str, Any]:
        """Publish a consistent read or let the caller retry while the catalog is changing."""
        with self.connection() as conn:
            revision = conn.execute("PRAGMA data_version").fetchone()[0]
            page = self.analysis(analysis)
            timeline = page["timeline"]
            offset = page["next_offset"]
            while offset is not None:
                more = self.analysis(analysis, offset)
                timeline.extend(more["timeline"])
                offset = more["next_offset"]
            asset = page["asset_id"]
            source = dict(
                conn.execute(
                    "SELECT asset_id,sha256,source_artifact_id,duration_ms,has_audio "
                    "FROM assets WHERE asset_id=?",
                    (asset,),
                ).fetchone()
            )
            limitations = [] if source["has_audio"] else ["no_audio"]
            gaps = []
            summary = conn.execute(
                "SELECT artifact_id,checksum FROM stage_results WHERE "
                "analysis_id=? AND asset_id=? AND stage='summarize' AND status='committed'",
                (analysis, asset),
            ).fetchone()
            if summary and self.files.verify_hash(summary[0], summary[1]):
                gaps = json.loads(self.files.read_bytes(summary[0]))["visual_coverage_gaps"]
            else:
                limitations.append("summary_unavailable")
            ids = {eid for entry in timeline for eid in entry["evidence_ids"]}
            questions = []
            for q, j, query, mode in conn.execute(
                "SELECT question_id,job_id,query_text,mode FROM questions "
                "JOIN jobs USING(question_id,analysis_id,asset_id) "
                "WHERE analysis_id=? AND asset_id=? "
                "ORDER BY questions.created_at,question_id",
                (analysis, asset),
            ):
                qdata = self.question(q)
                qdata.update(question_id=q, qa_job_id=j, query_text=query, mode=mode)
                if "answer" in qdata:
                    for claim in qdata["answer"]["claims"]:
                        ids.update(claim["evidence_ids"])
                    if event := qdata["answer"]["localization"]:
                        for side in ("start_bracket", "end_bracket"):
                            edge = event[side]
                            ids.update((edge["before_evidence_id"], edge["after_evidence_id"]))
                else:
                    qdata["usage"] = self.ledger(q).model_dump()
                questions.append(qdata)
            evidence = {}
            for eid in sorted(ids):
                value = resolve_evidence(self.db, self.files, asset, analysis, eid)
                if value is None:
                    limitations.append("missing_evidence:" + eid)
                    evidence[eid] = {"evidence_id": eid, "status": "missing"}
                else:
                    evidence[eid] = value.model_dump()
            document = {
                "schema_version": "1.1",
                "analysis_id": analysis,
                "source": source,
                "status": page["status"],
                "index_state": page["index_state"],
                "versions": {
                    "config_hash": page["config_hash"],
                    "prompt_bundle_hash": page["prompt_bundle_hash"],
                },
                "coverage": {"limitations": limitations, "visual_coverage_gaps": gaps},
                "timeline": timeline,
                "evidence": evidence,
                "questions": questions,
                "usage": self.ledger(analysis).model_dump(),
            }
            if revision != conn.execute("PRAGMA data_version").fetchone()[0]:
                raise StorageError(ErrorCode.RESOURCE_BUSY, "catalog changed during export")
            return document

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
                **{k: v for k, v in dict(row).items() if k in VideoAsset.model_fields},
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
