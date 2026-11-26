"""M1.5.3 development vertical-slice runner and diagnostic gate evaluator.

Runs the 1-3 dev videos pinned in eval/manifests/slice.jsonl through the M1.5
vertical slice pipeline, enforces strict separation between offline Fake runs
and authorized live runs, refuses dropped/reclassified failed questions, and
evaluates the M1.5-A2/M1.5-A3 continue/adjust/pause diagnostic gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import tempfile
import uuid
from collections.abc import Coroutine
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard

from eval.manifest import (
    ALLOWED_ANSWERABILITY,
    PRIVATE_PATH_PATTERN,
    REQUIRED_AUTH_FIELDS,
    SHA256_PATTERN,
    load_dev_seed_annotations,
)
from video_agent.application.slice import (
    SliceQuestionResult,
    SliceQuestionSpec,
    VerticalSliceResult,
    run_vertical_slice,
    slice_error_code,
)
from video_agent.bootstrap import build_slice_container
from video_agent.domain.models import UsageLedger

SliceExecutionMode = Literal["offline_fake", "live"]
SliceDecision = Literal["continue", "adjust_and_continue", "pause"]
SLICE_REPORT_SCHEMA_VERSION: str = "m1.5-slice-report-v2"
EXPECTED_QUESTIONS_PER_VIDEO: int = 4
MIN_SLICE_VIDEOS: int = 1
MAX_SLICE_VIDEOS: int = 3
ANSWERABLE_PASS_RATIO: float = 0.75


class SliceReportError(ValueError):
    """Raised when a slice manifest or diagnostic report violates M1.5 integrity rules."""


@dataclass(frozen=True, slots=True)
class SliceDiagnosticSummary:
    """Evaluated M1.5-A2/M1.5-A3 diagnostic gate metrics and decision."""

    execution_mode: SliceExecutionMode
    is_live_quality_evidence: bool
    can_claim_model_quality: bool
    total_videos: int
    total_questions: int
    total_answerable: int
    required_answerable_correct: int
    correct_answerable: int
    total_unanswerable: int
    abstained_unanswerable: int
    total_citations: int
    valid_citations: int
    citation_validity_rate: float | None
    incomplete_questions: int
    pending_manual_judgments: int
    failed_question_ids: list[str]
    quality_gate_passed: bool
    decision: SliceDecision
    decision_reason: str


def _is_strict_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_safe_relative_path(path_value: Any, field_name: str) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        raise SliceReportError(f"{field_name} must be a non-empty relative path string")
    if PRIVATE_PATH_PATTERN.search(path_value):
        raise SliceReportError(f"Private path detected in {field_name}: {path_value}")
    if path_value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path_value):
        raise SliceReportError(f"Absolute path forbidden in {field_name}: {path_value}")
    if ".." in Path(path_value).parts:
        raise SliceReportError(f"Parent traversal forbidden in {field_name}: {path_value}")
    return path_value


def _compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _validate_authorization(auth: Any, video_id: str) -> None:
    if not isinstance(auth, dict):
        raise SliceReportError(f"Video {video_id} missing authorization metadata")
    for field in REQUIRED_AUTH_FIELDS:
        if field not in auth:
            raise SliceReportError(f"Video {video_id} authorization missing field: {field}")
        value = auth[field]
        if field in ("commercial_use_allowed", "redistribution_allowed"):
            if not isinstance(value, bool):
                raise SliceReportError(f"Video {video_id} authorization.{field} must be boolean")
        elif not isinstance(value, str) or not value.strip():
            raise SliceReportError(
                f"Video {video_id} authorization.{field} must be non-empty string"
            )


def _run_offline_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive non-blocking offline coroutines without opening a Windows Proactor socketpair."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value  # type: ignore[no-any-return]
    coro.close()
    raise RuntimeError("offline slice coroutine unexpectedly suspended on event loop I/O")


def _safe_display_path(path: Path, repo_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
        return rel.as_posix()
    except ValueError:
        return path.name


def load_slice_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Load JSONL entries from eval/manifests/slice.jsonl."""
    if not manifest_path.is_file():
        raise SliceReportError(f"Slice manifest not found: {manifest_path}")
    entries: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SliceReportError(
                f"Invalid JSON on line {line_no} of {manifest_path.name}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SliceReportError(f"Line {line_no} of {manifest_path.name} must be a JSON object")
        entries.append(parsed)
    if not entries:
        raise SliceReportError(f"Slice manifest {manifest_path.name} is empty")
    return entries


def validate_slice_manifest(
    entries: list[dict[str, Any]],
    annotations: dict[str, Any],
    *,
    repo_root: Path | None = None,
    verify_files_on_disk: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Validate slice manifest against D0 annotations and return questions grouped by video_id."""
    if not (MIN_SLICE_VIDEOS <= len(entries) <= MAX_SLICE_VIDEOS):
        raise SliceReportError(
            f"M1.5 slice manifest requires {MIN_SLICE_VIDEOS}..{MAX_SLICE_VIDEOS} videos, "
            f"got {len(entries)}"
        )

    raw_questions = annotations.get("questions")
    if not isinstance(raw_questions, list):
        raise SliceReportError("Annotations payload must contain a 'questions' list")
    questions_by_id: dict[str, dict[str, Any]] = {}
    for q in raw_questions:
        if isinstance(q, dict) and isinstance(q.get("question_id"), str):
            questions_by_id[q["question_id"]] = q

    seen_video_ids: set[str] = set()
    seen_source_groups: set[str] = set()
    seen_question_ids: set[str] = set()
    grouped: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise SliceReportError("Every slice manifest entry requires a non-empty video_id")
        if video_id in seen_video_ids:
            raise SliceReportError(f"Duplicate video_id in slice manifest: {video_id}")
        seen_video_ids.add(video_id)

        if entry.get("split") != "m0_dev":
            raise SliceReportError(
                f"Video {video_id} in M1.5 slice must belong to split 'm0_dev', "
                f"got {entry.get('split')!r}"
            )
        if entry.get("m1_5_slice") is not True:
            raise SliceReportError(f"Video {video_id} must set m1_5_slice=true")

        source_group_id = entry.get("source_group_id")
        if not isinstance(source_group_id, str) or not source_group_id.strip():
            raise SliceReportError(f"Video {video_id} missing source_group_id")
        if source_group_id in seen_source_groups:
            raise SliceReportError(
                f"Duplicate source_group_id in slice manifest: {source_group_id}"
            )
        seen_source_groups.add(source_group_id)

        rel_path = _check_safe_relative_path(
            entry.get("relative_path"), f"{video_id}.relative_path"
        )
        _check_safe_relative_path(entry.get("annotations_path"), f"{video_id}.annotations_path")

        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise SliceReportError(f"Video {video_id} has invalid sha256: {sha256!r}")

        byte_size = entry.get("byte_size")
        if not _is_strict_int(byte_size) or byte_size <= 0:
            raise SliceReportError(f"Video {video_id} byte_size must be a positive integer")

        duration_ms = entry.get("duration_ms")
        if not _is_strict_int(duration_ms) or duration_ms <= 0:
            raise SliceReportError(f"Video {video_id} duration_ms must be a positive integer")

        _validate_authorization(entry.get("authorization"), video_id)

        qids = entry.get("question_ids")
        if (
            not isinstance(qids, list)
            or len(qids) != EXPECTED_QUESTIONS_PER_VIDEO
            or not all(isinstance(qid, str) and qid.strip() for qid in qids)
        ):
            raise SliceReportError(
                f"Video {video_id} must pin exactly {EXPECTED_QUESTIONS_PER_VIDEO} question_ids"
            )

        video_questions: list[dict[str, Any]] = []
        ans_count = 0
        unans_count = 0
        for qid in qids:
            if qid in seen_question_ids:
                raise SliceReportError(f"Duplicate pinned question_id across slice: {qid}")
            seen_question_ids.add(qid)
            q_meta = questions_by_id.get(qid)
            if q_meta is None:
                raise SliceReportError(f"Pinned question_id {qid!r} not found in D0 annotations")
            if q_meta.get("video_id") != video_id:
                raise SliceReportError(
                    f"Pinned question {qid} belongs to {q_meta.get('video_id')!r}, not {video_id!r}"
                )
            answerability = q_meta.get("answerability")
            if answerability not in ALLOWED_ANSWERABILITY:
                raise SliceReportError(f"Question {qid} has invalid answerability: {answerability}")
            if answerability == "answerable":
                ans_count += 1
            else:
                unans_count += 1
            video_questions.append(q_meta)

        question_mix = entry.get("question_mix")
        if question_mix is not None:
            if not isinstance(question_mix, dict):
                raise SliceReportError(f"Video {video_id} question_mix must be a dict")
            if (
                question_mix.get("answerable") != ans_count
                or question_mix.get("not_answerable") != unans_count
            ):
                raise SliceReportError(
                    f"Video {video_id} question_mix {question_mix} mismatches annotations "
                    f"(answerable={ans_count}, not_answerable={unans_count})"
                )

        if verify_files_on_disk:
            if repo_root is None:
                raise SliceReportError("repo_root is required when verify_files_on_disk=True")
            media_path = repo_root / rel_path
            if not media_path.is_file():
                raise SliceReportError(f"Video {video_id} missing on disk: {rel_path}")
            actual_size = media_path.stat().st_size
            if actual_size != byte_size:
                raise SliceReportError(
                    f"Video {video_id} byte_size mismatch: expected {byte_size}, got {actual_size}"
                )
            actual_sha = _compute_file_sha256(media_path)
            if actual_sha != sha256:
                raise SliceReportError(
                    f"Video {video_id} sha256 mismatch: expected {sha256}, got {actual_sha}"
                )

        grouped[video_id] = video_questions

    return grouped


def _gradeable_answer(item: dict[str, Any], total_citations: int) -> tuple[bool, bool]:
    """Check v1 summary counts and, when present, the underlying claims and gaps."""
    claims_count = item.get("claims_count")
    gaps_count = item.get("gaps_count")
    if not _is_strict_int(claims_count) or not _is_strict_int(gaps_count):
        return False, False
    if claims_count < 0 or gaps_count < 0:
        return False, False
    if "claims" in item:
        claims = item["claims"]
        if not isinstance(claims, list) or len(claims) != claims_count:
            return False, False
        references = 0
        for claim in claims:
            ids = claim.get("evidence_ids") if isinstance(claim, dict) else None
            if (
                not isinstance(ids, list)
                or not ids
                or not all(isinstance(i, str) and i.strip() for i in ids)
            ):
                return False, False
            references += len(ids)
        if references != total_citations:
            return False, False
    if "gaps" in item:
        gaps = item["gaps"]
        if not isinstance(gaps, list) or len(gaps) != gaps_count:
            return False, False
        has_gap = any(isinstance(g, str) and g.strip() for g in gaps)
    else:
        has_gap = gaps_count > 0
    status = item.get("answer_status")
    supported = 0 < claims_count <= total_citations
    return (
        supported and (status == "answered" or (status == "partial" and has_gap)),
        status == "not_answerable" and has_gap and claims_count == total_citations == 0,
    )


def evaluate_slice_report(
    report: dict[str, Any],
    *,
    manifest_entries: list[dict[str, Any]],
    annotations: dict[str, Any],
) -> SliceDiagnosticSummary:
    """Evaluate M1.5-A2/M1.5-A3 diagnostic gates while forbidding dropped or mixed results."""
    grouped = validate_slice_manifest(manifest_entries, annotations)

    if report.get("schema_version") not in ("m1.5-slice-report-v1", SLICE_REPORT_SCHEMA_VERSION):
        raise SliceReportError(
            f"Unsupported slice report schema_version: {report.get('schema_version')!r}"
        )

    execution_mode_raw = report.get("execution_mode")
    if execution_mode_raw not in ("offline_fake", "live"):
        raise SliceReportError(f"Invalid execution_mode: {execution_mode_raw!r}")
    execution_mode: SliceExecutionMode = execution_mode_raw

    is_live_evidence = report.get("is_live_quality_evidence")
    if not isinstance(is_live_evidence, bool):
        raise SliceReportError("is_live_quality_evidence must be a boolean")
    if execution_mode == "offline_fake" and is_live_evidence:
        raise SliceReportError(
            "offline_fake report cannot claim is_live_quality_evidence=True (Fake != live quality)"
        )
    if execution_mode == "live" and not is_live_evidence:
        raise SliceReportError("live report must set is_live_quality_evidence=True")

    reported_questions = report.get("questions")
    if not isinstance(reported_questions, list):
        raise SliceReportError("Report must contain a 'questions' list")

    expected_order: list[str] = []
    expected_by_qid: dict[str, dict[str, Any]] = {}
    for entry in manifest_entries:
        vid = str(entry["video_id"])
        for q_meta in grouped[vid]:
            qid = str(q_meta["question_id"])
            expected_order.append(qid)
            expected_by_qid[qid] = q_meta

    reported_by_qid: dict[str, dict[str, Any]] = {}
    for item in reported_questions:
        if not isinstance(item, dict):
            raise SliceReportError("Each question entry in report must be a dict")
        qid_raw = item.get("question_id")
        if not isinstance(qid_raw, str) or not qid_raw.strip():
            raise SliceReportError("Reported question missing question_id")
        if qid_raw in reported_by_qid:
            raise SliceReportError(f"Duplicate question_id in report: {qid_raw}")
        if qid_raw not in expected_by_qid:
            raise SliceReportError(f"Unexpected question_id not in pinned manifest: {qid_raw}")
        reported_by_qid[qid_raw] = item

    missing_qids = [qid for qid in expected_order if qid not in reported_by_qid]
    if missing_qids:
        raise SliceReportError(
            f"Report dropped {len(missing_qids)} pinned question(s): {missing_qids}"
        )

    total_answerable = 0
    correct_answerable = 0
    total_unanswerable = 0
    abstained_unanswerable = 0
    total_citations = 0
    valid_citations = 0
    pending_manual_judgments = 0
    incomplete_questions = 0
    inconsistent_rows = 0
    failed_question_ids: list[str] = []

    for qid in expected_order:
        expected = expected_by_qid[qid]
        item = reported_by_qid[qid]
        expected_vid = str(expected["video_id"])
        expected_ans = str(expected["answerability"])

        if item.get("video_id") != expected_vid:
            raise SliceReportError(
                f"Question {qid} video_id mismatch: expected {expected_vid!r}, "
                f"got {item.get('video_id')!r}"
            )
        if item.get("answerability") != expected_ans:
            raise SliceReportError(
                f"Post-hoc answerability reclassification forbidden for {qid}: "
                f"expected {expected_ans!r}, got {item.get('answerability')!r}"
            )
        if item.get("provider_mode") != execution_mode:
            raise SliceReportError(
                f"Mixed provider_mode forbidden in {execution_mode} report: "
                f"question {qid} had provider_mode={item.get('provider_mode')!r}"
            )

        q_total_cites = item.get("total_citations")
        q_valid_cites = item.get("valid_citations")
        if (
            not _is_strict_int(q_total_cites)
            or not _is_strict_int(q_valid_cites)
            or q_total_cites < 0
            or q_valid_cites < 0
            or q_valid_cites > q_total_cites
        ):
            raise SliceReportError(
                f"Question {qid} has invalid citation counts: "
                f"valid={q_valid_cites!r}, total={q_total_cites!r}"
            )

        total_citations += q_total_cites
        valid_citations += q_valid_cites
        citation_ok = q_valid_cites == q_total_cites

        judgment = item.get("manual_judgment")
        execution_status = item.get("execution_status", "completed")
        if execution_status not in ("completed", "failed", "not_run"):
            raise SliceReportError(f"Question {qid} has an invalid execution status")
        completed = execution_status == "completed" and not item.get("error_code")
        supported, abstained = _gradeable_answer(item, q_total_cites)
        grades = (
            ("correct", "incorrect")
            if expected_ans == "answerable"
            else ("abstained_ok", "hallucinated")
        )
        if not completed:
            incomplete_questions += 1
        elif (
            not (supported or abstained)
            or (judgment not in (None, "pending") and judgment not in grades)
            or (judgment == "correct" and not supported)
            or (judgment == "abstained_ok" and not abstained)
        ):
            inconsistent_rows += 1
        failed = not citation_ok or not completed

        if expected_ans == "answerable":
            total_answerable += 1
            if completed and judgment == "correct" and supported and citation_ok:
                correct_answerable += 1
            elif completed and judgment in (None, "pending"):
                pending_manual_judgments += 1
                failed = True
            else:
                failed = True
        else:
            total_unanswerable += 1
            if completed and judgment == "abstained_ok" and abstained and citation_ok:
                abstained_unanswerable += 1
            elif completed and judgment in (None, "pending"):
                pending_manual_judgments += 1
                failed = True
            else:
                failed = True

        if failed:
            failed_question_ids.append(qid)

    citation_validity_rate = valid_citations / total_citations if total_citations else None
    required_answerable_correct = math.ceil(total_answerable * ANSWERABLE_PASS_RATIO)
    failed_video = any(v.get("execution_status") == "failed" for v in report.get("videos", []))

    if execution_mode == "offline_fake":
        can_claim_model_quality = False
        quality_gate_passed = False
        decision: SliceDecision = "pause"
        decision_reason = (
            "offline_fake validates media, tokenizer, citation, and budget contracts only; "
            "live vision, QA and local ASR execution are required before claiming M1.5-A2 quality."
        )
    elif (
        failed_video or incomplete_questions or inconsistent_rows or citation_validity_rate is None
    ):
        can_claim_model_quality = False
        quality_gate_passed = False
        decision = "pause"
        decision_reason = (
            f"Video or evidence export failure: {failed_video}; "
            f"execution incomplete for {incomplete_questions} question(s), "
            f"inconsistent answer or grade for {inconsistent_rows}, "
            f"{total_citations} citation(s) available for review."
        )
    elif citation_validity_rate < 1.0:
        can_claim_model_quality = pending_manual_judgments == 0
        quality_gate_passed = False
        decision = "pause"
        decision_reason = (
            f"Citation validity rate {citation_validity_rate:.3f} < 1.000; "
            "grounding/citation defect blocks continuation."
        )
    elif pending_manual_judgments > 0:
        can_claim_model_quality = False
        quality_gate_passed = False
        decision = "pause"
        decision_reason = (
            f"{pending_manual_judgments} live question(s) still require manual review "
            "against D0 reference annotations before M1.5-A2 sign-off."
        )
    elif (
        correct_answerable >= required_answerable_correct
        and abstained_unanswerable == total_unanswerable
    ):
        can_claim_model_quality = True
        quality_gate_passed = True
        decision = "continue"
        decision_reason = (
            f"Live M1.5-A2 gate passed: {correct_answerable}/{total_answerable} answerable "
            f"correct (required >= {required_answerable_correct}), "
            f"{abstained_unanswerable}/{total_unanswerable} unanswerable abstained, "
            "100% valid citations."
        )
    else:
        can_claim_model_quality = True
        quality_gate_passed = False
        decision = "adjust_and_continue"
        decision_reason = (
            f"Live M1.5-A2 threshold not met ({correct_answerable}/{total_answerable} answerable "
            f"correct vs required {required_answerable_correct}; "
            f"{abstained_unanswerable}/{total_unanswerable} unanswerable abstained). "
            "Adjust prompts/sampling and re-run without dropping failed cases."
        )

    return SliceDiagnosticSummary(
        execution_mode=execution_mode,
        is_live_quality_evidence=is_live_evidence,
        can_claim_model_quality=can_claim_model_quality,
        total_videos=len(manifest_entries),
        total_questions=len(expected_order),
        total_answerable=total_answerable,
        required_answerable_correct=required_answerable_correct,
        correct_answerable=correct_answerable,
        total_unanswerable=total_unanswerable,
        abstained_unanswerable=abstained_unanswerable,
        total_citations=total_citations,
        valid_citations=valid_citations,
        citation_validity_rate=citation_validity_rate,
        incomplete_questions=incomplete_questions,
        pending_manual_judgments=pending_manual_judgments,
        failed_question_ids=failed_question_ids,
        quality_gate_passed=quality_gate_passed,
        decision=decision,
        decision_reason=decision_reason,
    )


def _public_text(value: str) -> str:
    return "[redacted local path]" if PRIVATE_PATH_PATTERN.search(value) else value


def _extract_ledger_metrics(ledger: UsageLedger) -> dict[str, Any]:
    input_tokens = sum(rec.input_tokens or 0 for rec in ledger.records)
    output_tokens = sum(rec.output_tokens or 0 for rec in ledger.records)
    unknown_cost_events = sum(
        1 for rec in ledger.records if rec.cost_status == "unknown" or rec.status == "unknown"
    )
    return {
        "provider_attempts": int(ledger.total_attempts),
        "images_sent": int(ledger.total_images_sent),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "confirmed_cost_usd": float(ledger.confirmed_cost_usd),
        "estimated_cost_usd": float(ledger.estimated_cost_usd),
        "cost_status": str(ledger.cost_status),
        "unknown_cost_events": int(unknown_cost_events),
    }


def _summarize_video_result(
    entry: dict[str, Any],
    result: VerticalSliceResult,
) -> dict[str, Any]:
    return {
        "video_id": entry["video_id"],
        "benchmark": entry["benchmark"],
        "relative_path": entry["relative_path"],
        "sha256": entry["sha256"],
        "duration_ms": result.asset.duration_ms,
        "manifest_duration_ms": entry["duration_ms"],
        "width": result.asset.width,
        "height": result.asset.height,
        "has_audio": result.asset.has_audio,
        "audio_status": result.audio_status,
        "frames_extracted": len(result.frames),
        "frame_pts_ms_sample": [f.actual_pts_ms for f in result.frames],
        "transcript_segments_count": len(result.transcript_segments),
        "observations_count": len(result.observations),
        "analysis_id": result.analysis_id,
        "prompt_bundle_hash": result.prompt_bundle_hash,
        "latency_ms": result.latency_ms,
        "analysis_usage": _extract_ledger_metrics(result.analysis_usage_ledger),
        "warnings": [_public_text(w) for w in result.warnings],
    }


def _question_row(
    meta: dict[str, Any],
    mode: SliceExecutionMode,
    result: SliceQuestionResult | None,
    status: str,
    error_code: str | None,
    hit_count: int,
    ledger: UsageLedger,
) -> dict[str, Any]:
    answer = result.answer if result else None
    claims = answer.claims if answer else []
    gaps = answer.gaps if answer else []
    ids = [eid for claim in claims for eid in claim.evidence_ids]
    return {
        "video_id": meta["video_id"],
        "question_id": meta["question_id"],
        "category": meta.get("category"),
        "answerability": meta["answerability"],
        "provider_mode": mode,
        "execution_status": status,
        "error_code": error_code,
        "search_hits_count": hit_count,
        "answer_status": answer.status if answer else None,
        "claims_count": len(claims),
        "gaps_count": len(gaps),
        "total_citations": len(ids),
        "valid_citations": sum(eid in result.resolved_evidence for eid in ids) if result else 0,
        "claims": [
            {
                **claim.model_dump(),
                "text": _public_text(claim.text),
                "limitations": [_public_text(note) for note in claim.limitations],
            }
            for claim in claims
        ],
        "gaps": [_public_text(gap) for gap in gaps],
        "manual_judgment": "pending",
        "qa_usage": _extract_ledger_metrics(ledger),
    }


def _write_report(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as staging:
        staged = Path(staging) / path.name
        staged.write_text(serialized, encoding="utf-8", newline="\n")
        staged.replace(path)


def run_slice_manifest(
    *,
    manifest_path: Path,
    mode: Literal["offline", "live"],
    data_root: Path,
    live_provider: Literal["deepseek", "openai"] | None = None,
    output_dir: Path | None = None,
    budget_config_path: Path | None = None,
    asr_model_path: Path | None = None,
    repo_root: Path | None = None,
    sample_interval_ms: int = 5000,
    max_frames: int = 12,
) -> dict[str, Any]:
    """Execute the pinned M1.5 slice manifest and return the sanitized diagnostic report."""
    resolved_repo_root = repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    entries = load_slice_manifest(manifest_path)

    ann_rel = _check_safe_relative_path(entries[0].get("annotations_path"), "annotations_path")
    annotations = load_dev_seed_annotations(resolved_repo_root / ann_rel)
    grouped = validate_slice_manifest(
        entries,
        annotations,
        repo_root=resolved_repo_root,
        verify_files_on_disk=True,
    )

    if mode == "live":
        if output_dir is None:
            raise SliceReportError("live mode requires explicit --output-dir")
        if budget_config_path is None:
            raise SliceReportError(
                "live mode requires --budget-config pointing to an approval YAML"
            )

    container = build_slice_container(
        mode=mode,
        data_root=data_root,
        live_provider=live_provider,
        repo_root=resolved_repo_root,
        budget_config_path=budget_config_path,
        asr_model_path=asr_model_path,
    )

    execution_mode: SliceExecutionMode = container.mode
    is_live_evidence = execution_mode == "live"

    videos_report: list[dict[str, Any]] = []
    questions_report: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex

    with closing(container):
        for entry in entries:
            video_id = str(entry["video_id"])
            source_file = resolved_repo_root / str(entry["relative_path"])
            pinned_questions = grouped[video_id]
            q_specs = [
                SliceQuestionSpec(
                    question_id=str(q["question_id"]),
                    query_text=str(q["question_text"]),
                )
                for q in pinned_questions
            ]
            analysis_id = f"an_slice_{video_id}_{run_id}"
            start_time = container.clock.monotonic()
            coro = run_vertical_slice(
                str(source_file),
                asset_id=f"asset_{video_id}",
                analysis_id=analysis_id,
                questions=q_specs,
                media_backend=container.media_backend,
                transcriber=container.transcriber,
                vision_provider=container.vision_provider,
                planner_provider=container.planner_provider,
                budget_guard=container.budget_guard,
                prompt_registry=container.prompt_registry,
                config=container.config,
                clock=container.clock,
                provider_mode=execution_mode,
                collect_question_errors=True,
                sample_interval_ms=sample_interval_ms,
                max_frames=max_frames,
            )
            result: VerticalSliceResult | None = None
            analysis_error: str | None = None
            try:
                result = asyncio.run(coro) if mode == "live" else _run_offline_coro(coro)
            except Exception as exc:
                analysis_error = slice_error_code(exc)

            if result is None:
                video: dict[str, Any] = {
                    "video_id": video_id,
                    "analysis_id": analysis_id,
                    "sha256": entry["sha256"],
                    "execution_status": "failed",
                    "error_code": analysis_error,
                    "latency_ms": max(0, int((container.clock.monotonic() - start_time) * 1000)),
                    "analysis_usage": _extract_ledger_metrics(
                        container.budget_guard.get_ledger(analysis_id)
                    ),
                }
            else:
                video = _summarize_video_result(entry, result)
                video["execution_status"] = "partial" if result.question_errors else "completed"
                try:
                    video["evidence_manifest"] = container.save_evidence_manifest(result)
                except Exception as exc:
                    video.update(execution_status="failed", error_code=slice_error_code(exc))
            videos_report.append(video)

            responses = (
                {qr.question.question_id: qr for qr in result.question_results} if result else {}
            )
            for meta in pinned_questions:
                qid = str(meta["question_id"])
                response = responses.get(qid)
                failure = result.question_errors.get(qid) if result else None
                if response is not None:
                    status, error_code, hits = "completed", None, len(response.search_hits)
                elif failure is not None:
                    status, error_code, hits = (
                        "failed",
                        failure.error_code,
                        len(failure.search_hits),
                    )
                else:
                    status, error_code, hits = "not_run", analysis_error or "INTERNAL_ERROR", 0
                questions_report.append(
                    _question_row(
                        meta,
                        execution_mode,
                        response,
                        status,
                        error_code,
                        hits,
                        container.budget_guard.get_ledger(qid),
                    )
                )

    usage = [v["analysis_usage"] for v in videos_report] + [q["qa_usage"] for q in questions_report]
    usage_totals = {
        key: sum(m[key] for m in usage)
        for key in (
            "provider_attempts",
            "images_sent",
            "input_tokens",
            "output_tokens",
            "confirmed_cost_usd",
            "estimated_cost_usd",
            "unknown_cost_events",
        )
    }
    usage_totals["cost_status"] = (
        "unknown"
        if any(m["cost_status"] == "unknown" for m in usage)
        else "estimated"
        if any(m["cost_status"] == "estimated" for m in usage)
        else "confirmed"
    )

    report: dict[str, Any] = {
        "schema_version": SLICE_REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "approved_limits": (
            container.budget_guard.run_limits.model_dump(exclude_none=True)
            if container.budget_guard.run_limits is not None
            else None
        ),
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "manifest_path": _safe_display_path(manifest_path, resolved_repo_root),
        "execution_mode": execution_mode,
        "vision_provider": container.vision_provider.provider_name,
        "vision_model": container.vision_provider.model_id,
        "qa_provider": container.planner_provider.provider_name,
        "qa_model": container.planner_provider.model_id,
        "is_live_quality_evidence": is_live_evidence,
        "videos": videos_report,
        "questions": questions_report,
        "search_summary": {
            "total_search_hits": sum(q["search_hits_count"] for q in questions_report),
            "questions_with_hits": sum(q["search_hits_count"] > 0 for q in questions_report),
            "total_questions": len(questions_report),
        },
        "usage_totals": usage_totals,
    }

    summary = evaluate_slice_report(
        report,
        manifest_entries=entries,
        annotations=annotations,
    )
    report["diagnostic_gate"] = asdict(summary)
    failed_ids = set(summary.failed_question_ids)
    report["failed_cases"] = [
        {
            "question_id": q["question_id"],
            "video_id": q["video_id"],
            "answerability": q["answerability"],
            "answer_status": q["answer_status"],
            "execution_status": q["execution_status"],
            "error_code": q["error_code"],
            "manual_judgment": q["manual_judgment"],
            "valid_citations": q["valid_citations"],
            "total_citations": q["total_citations"],
        }
        for q in questions_report
        if q["question_id"] in failed_ids
    ]

    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if PRIVATE_PATH_PATTERN.search(serialized):
        raise SliceReportError("Sanitization failure: private absolute path found in slice report")

    if output_dir is not None:
        filename = "slice_live_report.json" if mode == "live" else "slice_offline_report.json"
        if summary.incomplete_questions or any(
            v["execution_status"] == "failed" for v in videos_report
        ):
            _write_report(output_dir / "failed_runs" / run_id / filename, serialized)
        _write_report(output_dir / filename, serialized)

    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `python -m eval.slice_runner`."""
    parser = argparse.ArgumentParser(
        description="Run the M1.5 development vertical slice and evaluate diagnostic gates."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/manifests/slice.jsonl"),
        help="Path to the pinned M1.5 slice JSONL manifest.",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--offline",
        action="store_true",
        help="Run real media extraction with offline Fake providers (no live quality claim).",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Run with cloud vision/QA and local faster-whisper (requires budget and key).",
    )
    parser.add_argument("--provider", choices=["deepseek", "openai"], help="Explicit live provider")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write slice_offline_report.json or slice_live_report.json.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/slice_runner_work"),
        help="Local working directory for extracted slice artifacts.",
    )
    parser.add_argument(
        "--budget-config",
        type=Path,
        default=None,
        help="Run-wide budget YAML for live approval or offline rehearsal.",
    )
    parser.add_argument(
        "--asr-model-path",
        type=Path,
        default=None,
        help="Local directory for faster-whisper weights (used in --live mode).",
    )
    parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=5000,
        help="Frame sampling interval in milliseconds (default: 5000).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=12,
        help="Maximum frames sampled per video in M1.5 slice (default: 12).",
    )
    args = parser.parse_args(argv)

    run_mode: Literal["offline", "live"] = "live" if args.live else "offline"
    resolved_output_dir = (
        args.output_dir
        if args.output_dir is not None
        else (None if run_mode == "live" else Path("data/reports"))
    )

    report = run_slice_manifest(
        manifest_path=args.manifest,
        mode=run_mode,
        data_root=args.data_root,
        live_provider=args.provider,
        output_dir=resolved_output_dir,
        budget_config_path=args.budget_config,
        asr_model_path=args.asr_model_path,
        sample_interval_ms=args.sample_interval_ms,
        max_frames=args.max_frames,
    )
    gate = report["diagnostic_gate"]
    usage = report["usage_totals"]
    rate = gate["citation_validity_rate"]
    rate_label = f"{rate:.3f}" if rate is not None else "n/a"
    print(
        f"M1.5 slice finished: mode={report['execution_mode']}, "
        f"videos={gate['total_videos']}, questions={gate['total_questions']}, "
        f"citation_validity={rate_label}, "
        f"attempts={usage['provider_attempts']}, images={usage['images_sent']}, "
        f"can_claim_model_quality={gate['can_claim_model_quality']}, "
        f"decision={gate['decision']}"
    )
    return int(
        gate["incomplete_questions"] > 0
        or any(v["execution_status"] == "failed" for v in report["videos"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
