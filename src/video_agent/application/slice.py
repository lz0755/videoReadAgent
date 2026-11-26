"""Application-layer vertical slice orchestration (M1.5.1 / M1.5.2).

Depends exclusively on domain contracts, policies, pure tokenizer, and injected ports.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal
from typing import Literal

from video_agent.application.budget import BudgetGuard
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.domain.config import AppConfig
from video_agent.domain.errors import (
    BudgetError,
    DomainValidationError,
    ErrorCode,
    MediaError,
    ProviderError,
    VideoAgentError,
)
from video_agent.domain.models import (
    Answer,
    AudioTimelineMap,
    BudgetReservation,
    FrameEvidence,
    Observation,
    Question,
    RequestBudget,
    SupportKind,
    TimeRangeMs,
    TranscriptSegment,
    UsageLedger,
    VideoAsset,
)
from video_agent.domain.ports import (
    Clock,
    ImageInput,
    MediaBackend,
    PlannerProvider,
    PlannerRequest,
    PlannerResponse,
    SearchHit,
    Transcriber,
    TranscribeRequest,
    VisionProvider,
    VisionRequest,
    VisionResponse,
)
from video_agent.domain.tokenizer import (
    DEFAULT_SUBSTRING_FALLBACK_SCAN_LIMIT,
    TOKENIZER_VERSION,
    tokenize_text,
    validate_search_query,
)

AudioExtractionStatus = Literal["ok", "no_audio", "no_speech"]

_SLICE_TOOL_SCHEMA: dict[str, object] = {
    "slice_mode": "m1_5_vertical_slice",
    "tokenizer_version": TOKENIZER_VERSION,
    "tools": [],
}

_VISION_OUTPUT_SCHEMA: dict[str, object] = {
    "schema_version": "1.1",
    "type": "object",
    "required": ["observations"],
}

_ANSWER_OUTPUT_SCHEMA: dict[str, object] = {
    "schema_version": "1.1",
    "type": "object",
    "required": ["status", "claims", "gaps"],
}


@dataclass(frozen=True, slots=True)
class SliceMediaResult:
    """Output of the M1.5.1 thin media + ASR extraction pipeline."""

    asset: VideoAsset
    frames: list[FrameEvidence]
    audio_artifact_id: str | None
    audio_timeline: AudioTimelineMap | None
    transcript_segments: list[TranscriptSegment]
    audio_status: AudioExtractionStatus
    warnings: list[str] = field(default_factory=list)


def build_sample_timestamps_ms(
    duration_ms: int,
    *,
    interval_ms: int = 5000,
    max_frames: int = 16,
) -> list[int]:
    """Compute strictly integer requested timestamps within [0, duration_ms)."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise DomainValidationError(
            ErrorCode.INVALID_TIME_RANGE,
            "duration_ms must be a positive strict integer",
        )
    if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms <= 0:
        raise DomainValidationError(
            ErrorCode.INVALID_TIME_RANGE,
            "interval_ms must be a positive strict integer",
        )
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
        raise DomainValidationError(
            ErrorCode.INVALID_TIME_RANGE,
            "max_frames must be a positive strict integer",
        )

    # Real containers frequently report duration_ms slightly beyond the final frame's PTS
    # (e.g. ocr_941.mp4 duration_ms=30025 with last frame PTS < 30000ms). Reserve a small
    # tail margin so automatic uniform sampling does not request past the last real frame.
    tail_margin_ms = min(250, max(0, duration_ms - 1))
    effective_end_ms = max(1, duration_ms - tail_margin_ms)

    raw_times = list(range(0, effective_end_ms, interval_ms))
    if not raw_times:
        return [0]
    if len(raw_times) <= max_frames:
        return raw_times
    if max_frames == 1:
        return [0]

    step_span = (effective_end_ms - 1) // max_frames
    step = max(interval_ms, step_span)
    selected = [i * step for i in range(max_frames) if i * step < effective_end_ms]
    return selected or [0]


async def extract_slice_media(
    source_path: str,
    *,
    asset_id: str,
    media_backend: MediaBackend,
    transcriber: Transcriber,
    sample_interval_ms: int = 5000,
    max_frames: int = 16,
    language: str | None = None,
    remaining_asr_seconds: Callable[[], float] | None = None,
) -> SliceMediaResult:
    """Run real file -> PyAV frames + FFmpeg audio + Transcriber chain via ports."""
    asset = media_backend.probe_video(source_path, asset_id=asset_id)
    requested_times = build_sample_timestamps_ms(
        asset.duration_ms,
        interval_ms=sample_interval_ms,
        max_frames=max_frames,
    )
    frames = media_backend.extract_frames(
        asset,
        requested_times,
        range_ms=TimeRangeMs(start_ms=0, end_ms=asset.duration_ms),
    )

    warnings: list[str] = []
    if not asset.has_audio:
        warnings.append("no_audio_stream: proceeding with visual frames only")
        return SliceMediaResult(
            asset=asset,
            frames=frames,
            audio_artifact_id=None,
            audio_timeline=None,
            transcript_segments=[],
            audio_status="no_audio",
            warnings=warnings,
        )

    try:
        audio_artifact_id, audio_timeline = media_backend.extract_audio(asset)
    except MediaError as exc:
        if exc.code is ErrorCode.NO_AUDIO_STREAM:
            warnings.append("no_audio_stream: proceeding with visual frames only")
            return SliceMediaResult(
                asset=asset,
                frames=frames,
                audio_artifact_id=None,
                audio_timeline=None,
                transcript_segments=[],
                audio_status="no_audio",
                warnings=warnings,
            )
        raise

    asr_request = TranscribeRequest(
        asset_id=asset.asset_id,
        audio_artifact_id=audio_artifact_id,
        audio_timeline=audio_timeline,
        language=language,
        vad_filter=False,
    )
    if remaining_asr_seconds is None:
        asr_response = await transcriber.transcribe(asr_request)
    else:
        timeout = asyncio.timeout(remaining_asr_seconds())
        try:
            async with timeout:
                asr_response = await transcriber.transcribe(asr_request)
        except TimeoutError as exc:
            if not timeout.expired():
                raise
            raise MediaError(ErrorCode.DEADLINE_EXCEEDED, "local ASR deadline elapsed") from exc
        # A synchronous cache hit or CPU stage can finish without yielding to the timer.
        remaining_asr_seconds()
    audio_status: AudioExtractionStatus = "ok" if asr_response.segments else "no_speech"
    if audio_status == "no_speech":
        warnings.append("no_speech_detected: audio stream contained no transcribed segments")

    return SliceMediaResult(
        asset=asset,
        frames=frames,
        audio_artifact_id=audio_artifact_id,
        audio_timeline=audio_timeline,
        transcript_segments=list(asr_response.segments),
        audio_status=audio_status,
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class SliceQuestionSpec:
    """Question input to the M1.5 vertical slice pipeline."""

    question_id: str
    query_text: str


@dataclass(frozen=True, slots=True)
class SliceQuestionResult:
    """Traceable per-question output from the M1.5 vertical slice."""

    question: Question
    search_hits: list[SearchHit]
    answer: Answer
    resolved_evidence: dict[str, FrameEvidence | TranscriptSegment]
    qa_usage_ledger: UsageLedger


@dataclass(frozen=True, slots=True)
class SliceQuestionFailure:
    question: SliceQuestionSpec
    search_hits: list[SearchHit]
    error_code: str
    qa_usage_ledger: UsageLedger


def slice_error_code(exc: Exception) -> str:
    """Diagnostic files expose stable codes and omit exception text containing private inputs."""
    return exc.code.value if isinstance(exc, VideoAgentError) else "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class VerticalSliceResult:
    """Complete traceable output of a single video vertical slice execution."""

    asset: VideoAsset
    analysis_id: str
    prompt_bundle_hash: str
    provider_mode: Literal["offline_fake", "live"]
    frames: list[FrameEvidence]
    audio_artifact_id: str | None
    audio_timeline: AudioTimelineMap | None
    transcript_segments: list[TranscriptSegment]
    audio_status: AudioExtractionStatus
    observations: list[Observation]
    question_results: list[SliceQuestionResult]
    analysis_usage_ledger: UsageLedger
    latency_ms: int
    warnings: list[str] = field(default_factory=list)
    question_errors: dict[str, SliceQuestionFailure] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _IndexedDoc:
    doc_id: str
    asset_id: str
    analysis_id: str
    source_kind: Literal["transcript", "observation"]
    support_kind: SupportKind
    source_id: str
    text: str
    normalized_text: str
    tokens: frozenset[str]
    time_range: TimeRangeMs
    evidence_ids: tuple[str, ...]


def _ranges_overlap(a: TimeRangeMs, b: TimeRangeMs) -> bool:
    return a.start_ms < b.end_ms and b.start_ms < a.end_ms


_SECONDS = r"(?:seconds?|secs?|s)(?![a-z])|秒"
_NUMBER = r"[0-9]{1,9}(?:\.[0-9]{1,3})?"
_LOCATION = re.compile(
    rf"(?:(?<![a-z0-9])(?P<cue>at|around|near|between|from)\b\s*"
    rf"(?:(?:approximately|about)\s+)?|(?:在|从|第)\s*(?:大约|约)?)"
    rf"\s*(?P<first>{_NUMBER})\s*"
    rf"(?:(?:(?:{_SECONDS})\s*)?(?:[-–—]|\bto\b|\band\b|至|到|和)\s*"
    rf"(?P<last>{_NUMBER})\s*)?(?:{_SECONDS})"
)
_DURATION_PREFIX = re.compile(r"\b(?:for|lasts?|lasted|takes?|took|wait(?:s|ed|ing)?)\s*$")
_DURATION_SUFFIX = re.compile(r"\s*(?:(?:之|以)?内|long\b|in length\b)")
_NEARBY_MS = 1000


def _question_times(query: str) -> tuple[int, int] | None:
    """Recognize explicit second-based locations in the question, including decimal seconds."""
    for match in _LOCATION.finditer(query):
        if _DURATION_SUFFIX.match(query, match.end()):
            continue
        if match["cue"] in {"around", "near", "between"} and _DURATION_PREFIX.search(
            query[: match.start()]
        ):
            continue
        first = int(Decimal(match["first"]) * 1000)
        last = int(Decimal(match["last"] or match["first"]) * 1000)
        return min(first, last), max(first, last)
    return None


type _ScoredDoc = tuple[float, int, _IndexedDoc]


def _nearby_sources(ranked: list[_ScoredDoc], anchors: tuple[int, int]) -> list[_ScoredDoc]:
    """Keep a view and an original utterance at each requested endpoint within the hit budget."""

    def nearest_at(anchor: int) -> list[_ScoredDoc]:
        def distance(item: _ScoredDoc) -> tuple[int, float]:
            span = item[2].time_range
            return max(span.start_ms - anchor, anchor - (span.end_ms - 1), 0), -item[0]

        pictures = (hit for hit in ranked if hit[2].support_kind == "observed")
        speech = (hit for hit in ranked if hit[2].source_kind == "transcript")
        nearest = [min(group, key=distance, default=None) for group in (pictures, speech)]
        return sorted((hit for hit in nearest if hit is not None), key=distance)

    # First cover both times, then fill in the second modality and other lexical matches.
    by_anchor = [nearest_at(anchor) for anchor in dict.fromkeys(anchors)]
    order = [hits[i] for i in range(2) for hits in by_anchor if i < len(hits)]
    unique: dict[str, _ScoredDoc] = {}
    for item in order + ranked:
        unique.setdefault(item[2].doc_id, item)
    return list(unique.values())


class SliceSearchIndex:
    """In-memory M1.5.2 SearchPort using the shared pure domain tokenizer."""

    def __init__(self) -> None:
        self._docs: list[_IndexedDoc] = []

    def index_transcripts(
        self,
        *,
        asset_id: str,
        analysis_id: str,
        segments: list[TranscriptSegment],
    ) -> None:
        for seg in segments:
            if seg.asset_id != asset_id:
                raise DomainValidationError(
                    ErrorCode.CROSS_ASSET_REFERENCE,
                    f"transcript segment '{seg.evidence_id}' does not belong to asset '{asset_id}'",
                )
            text = seg.text.strip()
            if not text:
                continue
            self._docs.append(
                _IndexedDoc(
                    doc_id=f"doc_tr_{analysis_id}_{seg.evidence_id}",
                    asset_id=asset_id,
                    analysis_id=analysis_id,
                    source_kind="transcript",
                    support_kind="heard",
                    source_id=seg.evidence_id,
                    text=seg.text,
                    normalized_text=seg.text.lower(),
                    tokens=frozenset(tokenize_text(seg.text)),
                    time_range=seg.time_range,
                    evidence_ids=(seg.evidence_id,),
                )
            )

    def index_observations(
        self,
        *,
        asset_id: str,
        analysis_id: str,
        observations: list[Observation],
    ) -> None:
        for obs in observations:
            if obs.asset_id != asset_id or obs.analysis_id != analysis_id:
                raise DomainValidationError(
                    ErrorCode.CROSS_ASSET_REFERENCE,
                    (
                        f"observation '{obs.observation_id}' scope "
                        f"({obs.asset_id}/{obs.analysis_id}) mismatches ({asset_id}/{analysis_id})"
                    ),
                )
            self._docs.append(
                _IndexedDoc(
                    doc_id=f"doc_obs_{analysis_id}_{obs.observation_id}",
                    asset_id=asset_id,
                    analysis_id=analysis_id,
                    source_kind="observation",
                    support_kind=obs.kind,
                    source_id=obs.observation_id,
                    text=obs.text,
                    normalized_text=obs.text.lower(),
                    tokens=frozenset(tokenize_text(obs.text)),
                    time_range=obs.time_range,
                    evidence_ids=tuple(obs.evidence_ids),
                )
            )

    def search(
        self,
        asset_id: str,
        analysis_id: str,
        query: str,
        *,
        limit: int = 5,
        time_range: TimeRangeMs | None = None,
    ) -> list[SearchHit]:
        normalized_query, query_tokens, needs_fallback = validate_search_query(query)
        clamped_limit = max(1, min(10, limit))
        anchors = _question_times(normalized_query)
        if anchors is not None:
            start = max(0, anchors[0] - _NEARBY_MS)
            end = anchors[1] + _NEARBY_MS + 1
            if time_range is not None:
                start = max(start, time_range.start_ms)
                end = min(end, time_range.end_ms)
            if start >= end:
                return []
            time_range = TimeRangeMs(start_ms=start, end_ms=end)

        # Scope strictly to (asset_id, analysis_id) BEFORE ranking/limiting.
        scoped = [
            doc
            for doc in self._docs
            if doc.asset_id == asset_id
            and doc.analysis_id == analysis_id
            and (time_range is None or _ranges_overlap(doc.time_range, time_range))
        ]

        scored: list[_ScoredDoc] = []
        if needs_fallback:
            for doc in scoped[:DEFAULT_SUBSTRING_FALLBACK_SCAN_LIMIT]:
                if normalized_query in doc.normalized_text:
                    scored.append((1.0, doc.time_range.start_ms, doc))
        else:
            query_set = frozenset(query_tokens)
            for doc in scoped:
                matched = query_set & doc.tokens
                if not matched and anchors is None:
                    continue
                coverage = len(matched) / len(query_set)
                score = coverage + 0.05 * len(matched)
                scored.append((score, doc.time_range.start_ms, doc))

        scored.sort(key=lambda item: (-item[0], item[1]))
        if anchors is not None:
            scored = _nearby_sources(scored, anchors)
        return [
            SearchHit(
                doc_id=doc.doc_id,
                asset_id=doc.asset_id,
                analysis_id=doc.analysis_id,
                source_kind=doc.source_kind,
                source_id=doc.source_id,
                text=doc.text,
                time_range=doc.time_range,
                evidence_ids=list(doc.evidence_ids),
                score=score,
            )
            for score, _, doc in scored[:clamped_limit]
        ]


def validate_observations(
    observations: list[Observation],
    *,
    asset_id: str,
    analysis_id: str,
    valid_evidence_ids: set[str],
    other_asset_evidence_ids: set[str] | None = None,
) -> list[Observation]:
    """Reject any observation with wrong asset/analysis scope or fabricated citations."""
    other_ids = other_asset_evidence_ids or set()
    for obs in observations:
        if obs.asset_id != asset_id or obs.analysis_id != analysis_id:
            raise DomainValidationError(
                ErrorCode.CROSS_ASSET_REFERENCE,
                f"observation '{obs.observation_id}' has mismatched asset_id/analysis_id",
            )
        for ev_id in obs.evidence_ids:
            if ev_id in other_ids:
                raise DomainValidationError(
                    ErrorCode.CROSS_ASSET_REFERENCE,
                    f"observation '{obs.observation_id}' cites cross-asset evidence '{ev_id}'",
                )
            if ev_id not in valid_evidence_ids:
                raise DomainValidationError(
                    ErrorCode.INVALID_CITATION,
                    f"observation '{obs.observation_id}' cites non-existent evidence '{ev_id}'",
                )
    return observations


def validate_answer_citations(
    answer: Answer,
    *,
    asset_id: str,
    analysis_id: str,
    question_id: str,
    valid_evidence_ids: set[str],
    other_asset_evidence_ids: set[str] | None = None,
) -> Answer:
    """Reject any answer with wrong binding, cross-asset citation, or fabricated evidence_id."""
    if answer.asset_id != asset_id:
        raise DomainValidationError(
            ErrorCode.CROSS_ASSET_REFERENCE,
            f"answer '{answer.answer_id}' asset_id mismatch",
        )
    if answer.analysis_id != analysis_id or answer.question_id != question_id:
        raise DomainValidationError(
            ErrorCode.INVALID_CITATION,
            f"answer '{answer.answer_id}' analysis_id/question_id mismatch",
        )
    other_ids = other_asset_evidence_ids or set()
    for claim in answer.claims:
        for ev_id in claim.evidence_ids:
            if ev_id in other_ids:
                raise DomainValidationError(
                    ErrorCode.CROSS_ASSET_REFERENCE,
                    f"claim cites cross-asset evidence '{ev_id}'",
                )
            if ev_id not in valid_evidence_ids:
                raise DomainValidationError(
                    ErrorCode.INVALID_CITATION,
                    f"claim cites non-existent evidence '{ev_id}'",
                )
    return answer


def _format_utc(clock: Clock) -> str:
    return clock.now_utc().astimezone(UTC).isoformat().replace("+00:00", "Z")


def _settle_provider_error(
    budget_guard: BudgetGuard,
    reservation: BudgetReservation,
    exc: ProviderError,
) -> None:
    if exc.code in {ErrorCode.EXTERNAL_RESULT_UNKNOWN, ErrorCode.PROVIDER_TIMEOUT}:
        budget_guard.settle_unknown(reservation, error_code=exc.code)
    else:
        budget_guard.settle_failed(reservation, error_code=exc.code)


def _settle_slice_response(
    guard: BudgetGuard,
    reservation: BudgetReservation,
    response: VisionResponse | PlannerResponse,
    images_sent: int,
) -> None:
    if response.cost_status == "unknown":
        guard.settle_unknown(reservation, error_code=ErrorCode.EXTERNAL_RESULT_UNKNOWN)
        return
    confirmed_free = response.cost_status == "confirmed" and reservation.reserved_cost_usd == 0.0
    guard.settle_success(
        reservation,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        images_sent=images_sent,
        provider_request_id=response.provider_request_id,
        confirmed_cost_usd=0.0 if confirmed_free else None,
    )


async def _answer_slice_question(
    q_spec: SliceQuestionSpec,
    search_hits: list[SearchHit],
    *,
    asset_id: str,
    analysis_id: str,
    evidence_by_id: dict[str, FrameEvidence | TranscriptSegment],
    planner_provider: PlannerProvider,
    budget_guard: BudgetGuard,
    prompt_registry: PromptRegistry,
    clock: Clock,
) -> SliceQuestionResult:
    question = Question(
        question_id=q_spec.question_id,
        asset_id=asset_id,
        analysis_id=analysis_id,
        query_text=q_spec.query_text,
        mode="qa",
        created_at_utc=_format_utc(clock),
    )
    budget_guard.ensure_started(question.question_id, "qa")
    qa_job_id = f"job_qa_{uuid.uuid4().hex[:10]}"

    valid_evidence_ids = set(evidence_by_id)
    available_evidence_ids: list[str] = []
    seen_ev: set[str] = set()
    for hit in search_hits:
        for ev_id in hit.evidence_ids:
            if ev_id in valid_evidence_ids and ev_id not in seen_ev:
                seen_ev.add(ev_id)
                available_evidence_ids.append(ev_id)

    evidence_summary = (
        json.dumps(
            [
                {
                    "source_kind": hit.source_kind,
                    "source_id": hit.source_id,
                    "start_ms": hit.time_range.start_ms,
                    "end_ms": hit.time_range.end_ms,
                    "evidence_ids": hit.evidence_ids,
                    "text": hit.text,
                }
                for hit in search_hits
            ],
            ensure_ascii=False,
        )
        if search_hits
        else "No matching evidence found in indexed observations or transcripts."
    )

    rendered_answer = prompt_registry.render(
        "answer_compose",
        {
            "question_text": question.query_text,
            "evidence_summary": evidence_summary,
        },
        tool_schema=_SLICE_TOOL_SCHEMA,
        output_schema=_ANSWER_OUTPUT_SCHEMA,
    )

    qa_reservation = budget_guard.reserve_provider_attempt(
        RequestBudget(
            scope_id=question.question_id,
            job_id=qa_job_id,
            provider=planner_provider.provider_name,
            model_id=planner_provider.model_id,
            image_count=0,
            estimated_cost_usd=budget_guard.estimate_slice_request(
                planner_provider.model_id, rendered_answer.rendered_text, 0
            ),
            is_final_synthesis=True,
        )
    )
    planner_req = PlannerRequest(
        asset_id=asset_id,
        analysis_id=analysis_id,
        question_id=question.question_id,
        job_id=qa_job_id,
        prompt_text=rendered_answer.rendered_text,
        prompt_bundle_hash=rendered_answer.bundle_hash,
        available_evidence_ids=available_evidence_ids,
        timeout_seconds=qa_reservation.timeout_seconds,
        attempt_id=qa_reservation.attempt_id,
    )
    try:
        planner_resp = await planner_provider.decide(planner_req)
    except ProviderError as exc:
        _settle_provider_error(budget_guard, qa_reservation, exc)
        raise
    except BudgetError as exc:
        budget_guard.settle_not_sent(qa_reservation, error_code=exc.code)
        raise

    except Exception:
        budget_guard.settle_unknown(qa_reservation, error_code=ErrorCode.EXTERNAL_RESULT_UNKNOWN)
        raise

    _settle_slice_response(budget_guard, qa_reservation, planner_resp, 0)

    if planner_resp.answer is None:
        raise DomainValidationError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            f"planner returned action='{planner_resp.action}' without a final Answer",
        )

    validated_answer = validate_answer_citations(
        planner_resp.answer,
        asset_id=asset_id,
        analysis_id=analysis_id,
        question_id=question.question_id,
        valid_evidence_ids=valid_evidence_ids,
    )

    resolved_ev: dict[str, FrameEvidence | TranscriptSegment] = {}
    for claim in validated_answer.claims:
        for ev_id in claim.evidence_ids:
            resolved_ev[ev_id] = evidence_by_id[ev_id]

    return SliceQuestionResult(
        question=question,
        search_hits=search_hits,
        answer=validated_answer,
        resolved_evidence=resolved_ev,
        qa_usage_ledger=budget_guard.get_ledger(question.question_id),
    )


async def run_vertical_slice(
    source_path: str,
    *,
    asset_id: str,
    analysis_id: str,
    questions: list[SliceQuestionSpec],
    media_backend: MediaBackend,
    transcriber: Transcriber,
    vision_provider: VisionProvider,
    planner_provider: PlannerProvider,
    budget_guard: BudgetGuard,
    prompt_registry: PromptRegistry,
    config: AppConfig,
    clock: Clock,
    provider_mode: Literal["offline_fake", "live"] = "offline_fake",
    collect_question_errors: bool = False,
    sample_interval_ms: int = 5000,
    max_frames: int = 16,
    language: str | None = None,
) -> VerticalSliceResult:
    """Execute end-to-end M1.5.2 slice: media -> ASR -> vision -> search -> grounded QA."""
    started_mono = clock.monotonic()
    budget_guard.ensure_started(analysis_id, "analysis")
    analysis_job_id = f"job_an_{uuid.uuid4().hex[:10]}"

    media = await extract_slice_media(
        source_path,
        asset_id=asset_id,
        media_backend=media_backend,
        transcriber=transcriber,
        sample_interval_ms=sample_interval_ms,
        max_frames=max_frames,
        language=language,
        remaining_asr_seconds=(
            (lambda: budget_guard.remaining_seconds(analysis_id))
            if provider_mode == "live"
            else None
        ),
    )

    evidence_by_id: dict[str, FrameEvidence | TranscriptSegment] = {}
    for frame in media.frames:
        evidence_by_id[frame.evidence_id] = frame
    for seg in media.transcript_segments:
        evidence_by_id[seg.evidence_id] = seg

    # Stage 2: Multi-image visual observation in budgeted batches.
    batch_size = max(1, min(config.analysis.max_images_per_request, len(media.frames)))
    if (
        budget_guard.run_limits is not None
        and budget_guard.run_limits.max_images_per_request is not None
    ):
        batch_size = min(batch_size, budget_guard.run_limits.max_images_per_request)
    observations: list[Observation] = []
    prompt_bundle_hash = prompt_registry.compute_bundle_hash(
        tool_schema=_SLICE_TOOL_SCHEMA,
        output_schema=_VISION_OUTPUT_SCHEMA,
    )

    for offset in range(0, len(media.frames), batch_size):
        batch = media.frames[offset : offset + batch_size]
        window_start_ms = batch[0].actual_pts_ms
        window_end_ms = min(
            media.asset.duration_ms,
            max(batch[-1].actual_pts_ms + 1, window_start_ms + 1),
        )
        overlap_transcripts = [
            f"[{seg.evidence_id} {seg.time_range.start_ms}-{seg.time_range.end_ms}ms] {seg.text}"
            for seg in media.transcript_segments
            if _ranges_overlap(
                seg.time_range,
                TimeRangeMs(start_ms=window_start_ms, end_ms=window_end_ms),
            )
        ]
        frames_manifest = json.dumps(
            [
                {
                    "evidence_id": f.evidence_id,
                    "actual_pts_ms": f.actual_pts_ms,
                    "requested_ms": f.requested_ms,
                }
                for f in batch
            ],
            ensure_ascii=False,
        )
        rendered_vision = prompt_registry.render(
            "vision_observe",
            {
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                "frames_manifest": frames_manifest,
                "transcript_context": "\n".join(overlap_transcripts) or "(none)",
            },
            tool_schema=_SLICE_TOOL_SCHEMA,
            output_schema=_VISION_OUTPUT_SCHEMA,
        )
        prompt_bundle_hash = rendered_vision.bundle_hash

        reservation = budget_guard.reserve_provider_attempt(
            RequestBudget(
                scope_id=analysis_id,
                job_id=analysis_job_id,
                provider=vision_provider.provider_name,
                model_id=vision_provider.model_id,
                image_count=len(batch),
                estimated_cost_usd=budget_guard.estimate_slice_request(
                    vision_provider.model_id, rendered_vision.rendered_text, len(batch)
                ),
            )
        )
        vision_req = VisionRequest(
            asset_id=asset_id,
            analysis_id=analysis_id,
            job_id=analysis_job_id,
            prompt_text=rendered_vision.rendered_text,
            prompt_bundle_hash=rendered_vision.bundle_hash,
            images=[
                ImageInput(
                    evidence_id=f.evidence_id,
                    artifact_id=f.artifact_id,
                    mime_type="image/jpeg",
                    detail="high",
                    width=media.asset.width,
                    height=media.asset.height,
                    actual_pts_ms=f.actual_pts_ms,
                )
                for f in batch
            ],
            transcript_context=overlap_transcripts,
            timeout_seconds=reservation.timeout_seconds,
            attempt_id=reservation.attempt_id,
        )
        try:
            vision_resp = await vision_provider.observe(vision_req)
        except ProviderError as exc:
            _settle_provider_error(budget_guard, reservation, exc)
            raise
        except BudgetError as exc:
            budget_guard.settle_not_sent(reservation, error_code=exc.code)
            raise

        except Exception:
            budget_guard.settle_unknown(reservation, error_code=ErrorCode.EXTERNAL_RESULT_UNKNOWN)
            raise

        _settle_slice_response(budget_guard, reservation, vision_resp, len(batch))

        batch_valid_ids = {f.evidence_id for f in batch} | {
            seg.evidence_id for seg in media.transcript_segments
        }
        validated_batch_obs = validate_observations(
            vision_resp.observations,
            asset_id=asset_id,
            analysis_id=analysis_id,
            valid_evidence_ids=batch_valid_ids,
        )
        observations.extend(validated_batch_obs)

    # Stage 3: Build minimal in-memory Chinese bigram + English search index.
    search_index = SliceSearchIndex()
    search_index.index_transcripts(
        asset_id=asset_id,
        analysis_id=analysis_id,
        segments=media.transcript_segments,
    )
    search_index.index_observations(
        asset_id=asset_id,
        analysis_id=analysis_id,
        observations=observations,
    )

    # Interactive callers fail fast. The manifest evaluator keeps every pinned question.
    question_results: list[SliceQuestionResult] = []
    question_errors: dict[str, SliceQuestionFailure] = {}
    for q_spec in questions:
        hits: list[SearchHit] = []
        try:
            hits = search_index.search(asset_id, analysis_id, q_spec.query_text, limit=5)
            answer = await _answer_slice_question(
                q_spec,
                hits,
                asset_id=asset_id,
                analysis_id=analysis_id,
                evidence_by_id=evidence_by_id,
                planner_provider=planner_provider,
                budget_guard=budget_guard,
                prompt_registry=prompt_registry,
                clock=clock,
            )
        except Exception as exc:
            if not collect_question_errors:
                raise
            question_errors[q_spec.question_id] = SliceQuestionFailure(
                q_spec, hits, slice_error_code(exc), budget_guard.get_ledger(q_spec.question_id)
            )
        else:
            question_results.append(answer)

    elapsed_ms = max(0, int((clock.monotonic() - started_mono) * 1000))
    return VerticalSliceResult(
        asset=media.asset,
        analysis_id=analysis_id,
        prompt_bundle_hash=prompt_bundle_hash,
        provider_mode=provider_mode,
        frames=media.frames,
        audio_artifact_id=media.audio_artifact_id,
        audio_timeline=media.audio_timeline,
        transcript_segments=media.transcript_segments,
        audio_status=media.audio_status,
        observations=observations,
        question_results=question_results,
        analysis_usage_ledger=budget_guard.get_ledger(analysis_id),
        latency_ms=elapsed_ms,
        warnings=list(media.warnings),
        question_errors=question_errors,
    )
