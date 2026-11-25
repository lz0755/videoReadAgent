"""Strict domain models for Video Understanding Agent (M1.2).

Pure Pydantic v2 contracts only; zero infrastructure, SDK, media, HTTP, or DB imports.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from video_agent.domain.errors import ErrorCode

SchemaVersion = Literal["1.1"]
RegionId = Literal[
    "full",
    "r1c1",
    "r1c2",
    "r1c3",
    "r2c1",
    "r2c2",
    "r2c3",
    "r3c1",
    "r3c2",
    "r3c3",
]
SupportKind = Literal["observed", "heard", "inferred"]
CostStatus = Literal["estimated", "confirmed", "unknown"]
JobKind = Literal["analysis", "qa"]
JobStatus = Literal["queued", "running", "partial", "succeeded", "failed", "cancelled"]
AnswerStatus = Literal["answered", "partial", "not_answerable"]
QuestionMode = Literal["qa", "event_localization"]

NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]


def _validate_finite_non_negative_float(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cost must be a finite non-negative number")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float < 0.0:
        raise ValueError("cost must be a finite non-negative number")
    return as_float


class StrictModel(BaseModel):
    """Strict immutable base model forbidding coercion and unknown fields."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


class RationalTime(StrictModel):
    """Exact rational representation (numerator / denominator) for media time_base."""

    numerator: StrictInt = Field(gt=0)
    denominator: StrictInt = Field(gt=0)

    def to_fraction(self) -> Fraction:
        """Convert to Python exact Fraction."""
        return Fraction(self.numerator, self.denominator)


class TimeRangeMs(StrictModel):
    """Strict integer millisecond half-open range [start_ms, end_ms)."""

    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _validate_ordered(self) -> TimeRangeMs:
        if self.end_ms <= self.start_ms:
            raise ValueError(ErrorCode.INVALID_TIME_RANGE.value)
        return self

    def validate_within(self, duration_ms: int) -> None:
        """Ensure this range lies within [0, duration_ms]."""
        if self.end_ms > duration_ms:
            raise ValueError(ErrorCode.INVALID_TIME_RANGE.value)


def compute_canonical_pts_ms(
    *,
    raw_pts: int,
    time_base_num: int,
    time_base_den: int,
    origin_pts: int,
    origin_time_base_num: int,
    origin_time_base_den: int,
    duration_ms: int | None = None,
    range_ms: TimeRangeMs | None = None,
) -> int:
    """Compute canonical millisecond PTS by exact rational comparison before flooring.

    Evaluates exact `(raw_pts * time_base - origin_pts * origin_time_base) * 1000`,
    validates half-open bounds BEFORE flooring so 999.9 ms stays inside [0, 1000)
    and floors to 999 while 1000.0 ms is rejected.
    """
    for value in (
        raw_pts,
        time_base_num,
        time_base_den,
        origin_pts,
        origin_time_base_num,
        origin_time_base_den,
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)
    if (
        time_base_num <= 0
        or time_base_den <= 0
        or origin_time_base_num <= 0
        or origin_time_base_den <= 0
    ):
        raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)

    frame_sec = Fraction(raw_pts * time_base_num, time_base_den)
    origin_sec = Fraction(origin_pts * origin_time_base_num, origin_time_base_den)
    canonical_ms_exact = (frame_sec - origin_sec) * 1000

    if canonical_ms_exact < 0:
        raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)
    if duration_ms is not None and canonical_ms_exact >= Fraction(duration_ms, 1):
        raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)
    if range_ms is not None and not (
        Fraction(range_ms.start_ms, 1) <= canonical_ms_exact < Fraction(range_ms.end_ms, 1)
    ):
        raise ValueError(ErrorCode.NO_FRAME_IN_RANGE.value)

    return math.floor(canonical_ms_exact)


class CropBox(StrictModel):
    """Server-computed crop box in post-rotation display coordinates."""

    region_id: RegionId
    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)


class VideoAsset(StrictModel):
    """Registered local authorized video asset."""

    schema_version: SchemaVersion = "1.1"
    asset_id: NonEmptyStr
    source_artifact_id: NonEmptyStr | None = None
    sha256: Sha256Hex
    size_bytes: StrictInt = Field(gt=0)
    duration_ms: StrictInt = Field(gt=0)
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    video_codec: NonEmptyStr
    has_audio: StrictBool
    audio_codec: NonEmptyStr | None = None
    display_filename: NonEmptyStr
    status: Literal["importing", "ready", "failed"] = "importing"

    @model_validator(mode="after")
    def _validate_ready_source(self) -> VideoAsset:
        if self.status == "ready" and self.source_artifact_id is None:
            raise ValueError("ready VideoAsset must bind source_artifact_id")
        return self


class FrameEvidence(StrictModel):
    """Point-in-time frame evidence with traceable raw PTS, time_base, and origin."""

    schema_version: SchemaVersion = "1.1"
    evidence_id: NonEmptyStr
    asset_id: NonEmptyStr
    artifact_id: NonEmptyStr
    requested_ms: StrictInt = Field(ge=0)
    actual_pts_ms: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(gt=0)
    raw_pts: StrictInt
    time_base: RationalTime
    origin_pts: StrictInt
    origin_time_base: RationalTime
    crop: CropBox | None = None
    rotation_deg: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def _validate_pts_consistency(self) -> FrameEvidence:
        if self.actual_pts_ms >= self.duration_ms:
            raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)
        computed_ms = compute_canonical_pts_ms(
            raw_pts=self.raw_pts,
            time_base_num=self.time_base.numerator,
            time_base_den=self.time_base.denominator,
            origin_pts=self.origin_pts,
            origin_time_base_num=self.origin_time_base.numerator,
            origin_time_base_den=self.origin_time_base.denominator,
            duration_ms=self.duration_ms,
        )
        if computed_ms != self.actual_pts_ms:
            raise ValueError(ErrorCode.INVALID_TIMESTAMP.value)
        return self


class AudioTimelineMap(StrictModel):
    """Mapping from extracted audio stream back to canonical video origin."""

    origin_pts: StrictInt
    origin_time_base: RationalTime
    audio_offset_ms: StrictInt
    extracted_start_ms: StrictInt = Field(ge=0)
    leading_silence_padded: StrictBool = False
    vad_filter: Literal[False] = False


class TranscriptSegment(StrictModel):
    """Original ASR transcript segment anchored to canonical video timeline."""

    schema_version: SchemaVersion = "1.1"
    evidence_id: NonEmptyStr
    asset_id: NonEmptyStr
    time_range: TimeRangeMs
    text: str
    asr_version: NonEmptyStr
    audio_timeline: AudioTimelineMap
    language: str | None = None
    revisions: list[str] = Field(default_factory=list)


class Observation(StrictModel):
    """Structured observation tied to an analysis_id and existing evidence IDs."""

    schema_version: SchemaVersion = "1.1"
    observation_id: NonEmptyStr
    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    kind: SupportKind
    text: NonEmptyStr
    time_range: TimeRangeMs
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    region_hint: RegionId | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class Question(StrictModel):
    """Persisted user question bound to an asset_id and analysis_id."""

    schema_version: SchemaVersion = "1.1"
    question_id: NonEmptyStr
    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    query_text: NonEmptyStr
    mode: QuestionMode = "qa"
    created_at_utc: NonEmptyStr


class Claim(StrictModel):
    """Factual or inferred claim backed by explicit evidence IDs."""

    text: NonEmptyStr
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    support_kind: SupportKind
    limitations: list[str] = Field(default_factory=list)


class BoundaryBracket(StrictModel):
    """Closed bracket [lower_ms, upper_ms] around a visible event boundary."""

    lower_ms: StrictInt = Field(ge=0)
    upper_ms: StrictInt = Field(gt=0)
    duration_ms: StrictInt = Field(gt=0)
    before_evidence_id: NonEmptyStr
    after_evidence_id: NonEmptyStr
    estimate_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _validate_bracket(self) -> BoundaryBracket:
        if self.lower_ms >= self.upper_ms or self.upper_ms >= self.duration_ms:
            raise ValueError(ErrorCode.INVALID_TIME_RANGE.value)
        if not (self.lower_ms <= self.estimate_ms <= self.upper_ms):
            raise ValueError(ErrorCode.INVALID_TIME_RANGE.value)
        if self.before_evidence_id == self.after_evidence_id:
            raise ValueError(ErrorCode.INVALID_CITATION.value)
        return self


class EventLocalization(StrictModel):
    """Two-stage refined event start/end boundary localization."""

    label: NonEmptyStr
    start_bracket: BoundaryBracket
    end_bracket: BoundaryBracket
    refined: StrictBool = False

    @model_validator(mode="after")
    def _validate_order(self) -> EventLocalization:
        if self.start_bracket.estimate_ms >= self.end_bracket.estimate_ms:
            raise ValueError(ErrorCode.INVALID_TIME_RANGE.value)
        return self


class Answer(StrictModel):
    """Validated answer bound to asset_id, analysis_id, and question_id."""

    schema_version: SchemaVersion = "1.1"
    answer_id: NonEmptyStr
    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    question_id: NonEmptyStr
    status: AnswerStatus
    claims: list[Claim]
    gaps: list[str]
    trace_id: NonEmptyStr
    localization: EventLocalization | None = None

    @model_validator(mode="after")
    def _validate_status_invariants(self) -> Answer:
        non_blank_gaps = [g for g in self.gaps if g.strip()]
        if self.status in {"answered", "partial"} and not self.claims:
            raise ValueError(ErrorCode.SUPPORTED_CLAIM_REQUIRED.value)
        if self.status in {"partial", "not_answerable"} and not non_blank_gaps:
            raise ValueError(ErrorCode.GAP_REQUIRED.value)
        if self.status == "not_answerable" and self.claims:
            raise ValueError("not_answerable must not contain fabricated claims")
        return self


class Job(StrictModel):
    """Single-worker queued/running execution unit."""

    schema_version: SchemaVersion = "1.1"
    job_id: NonEmptyStr
    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    question_id: NonEmptyStr | None = None
    kind: JobKind
    status: JobStatus = "queued"
    cancel_requested: StrictBool = False
    attempt_no: StrictInt = Field(default=0, ge=0)
    idempotency_key: NonEmptyStr | None = None
    started_at_utc: str | None = None
    deadline_at_utc: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def _validate_kind_question_coupling(self) -> Job:
        if self.kind == "analysis" and self.question_id is not None:
            raise ValueError("analysis job must not have question_id")
        if self.kind == "qa" and self.question_id is None:
            raise ValueError("qa job requires question_id")
        return self


class PlaybackVerificationRecord(StrictModel):
    """Single verified source-vs-proxy timestamp comparison at start, mid, or end."""

    position: Literal["start", "mid", "end"]
    source_pts_ms: StrictInt = Field(ge=0)
    proxy_pts_ms: StrictInt = Field(ge=0)
    observed_offset_ms: StrictInt
    tolerance_ms: StrictInt = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _validate_observed_offset(self) -> PlaybackVerificationRecord:
        if self.observed_offset_ms != self.proxy_pts_ms - self.source_pts_ms:
            raise ValueError("observed_offset_ms must equal proxy_pts_ms - source_pts_ms")
        return self


class PlaybackTimelineMap(StrictModel):
    """Verified constant offset playback map or explicit preview_only fallback."""

    schema_version: SchemaVersion = "1.1"
    asset_id: NonEmptyStr
    artifact_id: NonEmptyStr
    status: Literal["validated_offset", "preview_only"]
    offset_ms: StrictInt | None = None
    verification_records: list[PlaybackVerificationRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_playback_status(self) -> PlaybackTimelineMap:
        if self.status == "validated_offset":
            if self.offset_ms is None:
                raise ValueError("validated_offset requires offset_ms")
            by_position: dict[str, PlaybackVerificationRecord] = {}
            for rec in self.verification_records:
                if rec.position in by_position:
                    raise ValueError(f"duplicate verification position '{rec.position}'")
                if abs(rec.observed_offset_ms - self.offset_ms) > rec.tolerance_ms:
                    raise ValueError(
                        "verification record observed_offset_ms exceeds allowed tolerance"
                    )
                by_position[rec.position] = rec
            if set(by_position.keys()) != {"start", "mid", "end"}:
                raise ValueError(
                    "validated_offset requires start, mid, and end verification records"
                )
            if not (
                by_position["start"].source_pts_ms
                < by_position["mid"].source_pts_ms
                < by_position["end"].source_pts_ms
            ):
                raise ValueError(
                    "start/mid/end verification records must have strictly increasing source_pts_ms"
                )
        elif self.offset_ms is not None:
            raise ValueError("preview_only must not declare an unverified offset_ms")
        return self


class BudgetLimits(StrictModel):
    """Hard execution limits for a single analysis_id or question_id budget scope."""

    scope_kind: JobKind
    max_rounds: StrictInt | None = Field(default=None, gt=0)
    max_tool_calls: StrictInt | None = Field(default=None, gt=0)
    max_expensive_tool_calls: StrictInt | None = Field(default=None, gt=0)
    max_unique_sampled_frames: StrictInt | None = Field(default=None, gt=0)
    max_submitted_image_parts: StrictInt = Field(gt=0)
    max_images_per_request: StrictInt = Field(gt=0)
    max_provider_attempts: StrictInt = Field(gt=0)
    deadline_seconds: StrictInt = Field(gt=0)
    request_timeout_seconds: StrictInt = Field(default=45, gt=0)
    max_transient_retries: StrictInt = Field(default=2, ge=0)
    max_schema_repairs: StrictInt = Field(default=1, ge=0)
    reserve_final_synthesis_attempt: StrictBool = False


class BudgetCounters(StrictModel):
    """Non-negative counters for reserved or consumed budget dimensions."""

    rounds: StrictInt = Field(default=0, ge=0)
    tool_calls: StrictInt = Field(default=0, ge=0)
    expensive_tool_calls: StrictInt = Field(default=0, ge=0)
    unique_sampled_frames: StrictInt = Field(default=0, ge=0)
    submitted_image_parts: StrictInt = Field(default=0, ge=0)
    provider_attempts: StrictInt = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    confirmed_cost_usd: float = Field(default=0.0, ge=0.0)

    @field_validator("estimated_cost_usd", "confirmed_cost_usd")
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        checked = _validate_finite_non_negative_float(value)
        assert checked is not None
        return checked


class RequestBudget(StrictModel):
    """Per-request budget reservation request sent to BudgetGuard."""

    scope_id: NonEmptyStr
    job_id: NonEmptyStr
    provider: NonEmptyStr
    model_id: NonEmptyStr
    image_count: StrictInt = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    parent_tool_call_id: NonEmptyStr | None = None
    is_final_synthesis: StrictBool = False

    @field_validator("estimated_cost_usd")
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        checked = _validate_finite_non_negative_float(value)
        assert checked is not None
        return checked


class BudgetReservation(StrictModel):
    """Immutable reservation token returned before a provider network attempt."""

    attempt_id: NonEmptyStr
    scope_id: NonEmptyStr
    job_id: NonEmptyStr
    provider: NonEmptyStr
    model_id: NonEmptyStr
    parent_tool_call_id: NonEmptyStr | None = None
    reserved_images: StrictInt = Field(ge=0)
    reserved_cost_usd: float = Field(ge=0.0)
    timeout_seconds: float = Field(gt=0.0)
    is_final_synthesis: StrictBool = False

    @field_validator("reserved_cost_usd")
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        checked = _validate_finite_non_negative_float(value)
        assert checked is not None
        return checked


class BudgetAccount(StrictModel):
    """Authoritative state of a budget scope across reservations and settlements."""

    schema_version: SchemaVersion = "1.1"
    scope_id: NonEmptyStr
    scope_kind: JobKind
    limits: BudgetLimits
    reserved: BudgetCounters = Field(default_factory=BudgetCounters)
    consumed: BudgetCounters = Field(default_factory=BudgetCounters)
    started_at_utc: str | None = None
    deadline_at_utc: str | None = None
    cancelled: StrictBool = False
    has_unknown_external_result: StrictBool = False


class UsageRecord(StrictModel):
    """Audit record for a single provider network attempt."""

    schema_version: SchemaVersion = "1.1"
    attempt_id: NonEmptyStr
    job_id: NonEmptyStr
    scope_id: NonEmptyStr
    parent_tool_call_id: NonEmptyStr | None = None
    provider: NonEmptyStr
    model_id: NonEmptyStr
    status: Literal["pending", "succeeded", "failed", "not_sent", "unknown"]
    reserved_images: StrictInt = Field(default=0, ge=0)
    images_sent: StrictInt = Field(default=0, ge=0)
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    confirmed_cost_usd: float | None = Field(default=None, ge=0.0)
    cost_status: CostStatus
    provider_request_id: str | None = None
    error_code: str | None = None

    @field_validator("estimated_cost_usd", "confirmed_cost_usd")
    @classmethod
    def _finite_cost(cls, value: float | None) -> float | None:
        return _validate_finite_non_negative_float(value)


class UsageLedger(StrictModel):
    """Aggregated view of a budget scope's attempts and cost status."""

    schema_version: SchemaVersion = "1.1"
    scope_id: NonEmptyStr
    records: list[UsageRecord] = Field(default_factory=list)
    reserved_attempts: StrictInt = Field(default=0, ge=0)
    reserved_images: StrictInt = Field(default=0, ge=0)
    total_attempts: StrictInt = Field(default=0, ge=0)
    total_images_sent: StrictInt = Field(default=0, ge=0)
    confirmed_cost_usd: float = Field(default=0.0, ge=0.0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    cost_status: CostStatus = "confirmed"

    @field_validator("confirmed_cost_usd", "estimated_cost_usd")
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        checked = _validate_finite_non_negative_float(value)
        assert checked is not None
        return checked
