"""Domain ports (Protocols) and transport-agnostic DTOs (M1.3).

Contains no provider SDK types, HTTP clients, media libraries, or database drivers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, StrictInt

from video_agent.domain.models import (
    Answer,
    AudioTimelineMap,
    BudgetAccount,
    BudgetLimits,
    CostStatus,
    FrameEvidence,
    NonEmptyStr,
    Observation,
    StrictModel,
    TimeRangeMs,
    TranscriptSegment,
    UsageRecord,
    VideoAsset,
)


class ImageInput(StrictModel):
    """Domain-level image metadata passed into VisionProvider."""

    evidence_id: NonEmptyStr
    artifact_id: NonEmptyStr
    mime_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    detail: Literal["low", "high", "auto"] = "high"
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    actual_pts_ms: StrictInt = Field(ge=0)


class VisionRequest(StrictModel):
    """Transport-agnostic request to VisionProvider."""

    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    job_id: NonEmptyStr
    prompt_text: NonEmptyStr
    prompt_bundle_hash: NonEmptyStr
    images: list[ImageInput] = Field(min_length=1)
    transcript_context: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=45.0, gt=0.0)
    attempt_id: NonEmptyStr | None = None


class VisionResponse(StrictModel):
    """Normalized response from VisionProvider."""

    observations: list[Observation]
    raw_text: str
    provider: NonEmptyStr
    model_id: NonEmptyStr
    provider_request_id: NonEmptyStr
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    cost_status: CostStatus


class ToolCallProposal(StrictModel):
    """Whitelisted tool call proposed by PlannerProvider."""

    tool_call_id: NonEmptyStr
    tool_name: Literal["search_evidence", "inspect_clip", "compare_frames"]
    arguments: dict[str, object] = Field(default_factory=dict)


class PlannerRequest(StrictModel):
    """Transport-agnostic request to PlannerProvider."""

    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    question_id: NonEmptyStr
    job_id: NonEmptyStr
    prompt_text: NonEmptyStr
    prompt_bundle_hash: NonEmptyStr
    available_evidence_ids: list[NonEmptyStr] = Field(default_factory=list)
    timeout_seconds: float = Field(default=45.0, gt=0.0)
    attempt_id: NonEmptyStr | None = None


class PlannerResponse(StrictModel):
    """Normalized response from PlannerProvider."""

    action: Literal["call_tools", "final_answer"]
    tool_calls: list[ToolCallProposal] = Field(default_factory=list)
    answer: Answer | None = None
    raw_text: str
    provider: NonEmptyStr
    model_id: NonEmptyStr
    provider_request_id: NonEmptyStr
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    cost_status: CostStatus


class TranscribeRequest(StrictModel):
    """Request to Transcriber port; P0 strictly forbids vad_filter=True."""

    asset_id: NonEmptyStr
    audio_artifact_id: NonEmptyStr
    audio_timeline: AudioTimelineMap
    language: str | None = None
    vad_filter: Literal[False] = False


class TranscribeResponse(StrictModel):
    """Normalized response from Transcriber port."""

    segments: list[TranscriptSegment]
    asr_version: NonEmptyStr
    language: str | None = None


class SearchHit(StrictModel):
    """Single indexed evidence hit returned by SearchPort."""

    doc_id: NonEmptyStr
    asset_id: NonEmptyStr
    analysis_id: NonEmptyStr
    source_kind: Literal["transcript", "observation"]
    source_id: NonEmptyStr
    text: NonEmptyStr
    time_range: TimeRangeMs
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    score: float


@runtime_checkable
class Clock(Protocol):
    """Injectable UTC wall clock + monotonic timer."""

    def now_utc(self) -> datetime: ...
    def monotonic(self) -> float: ...


@runtime_checkable
class MediaBackend(Protocol):
    """Local media probing, PTS frame extraction, and audio extraction port."""

    def probe_video(self, source_path: str, *, asset_id: str) -> VideoAsset: ...
    def extract_frames(
        self,
        asset: VideoAsset,
        requested_times_ms: list[int],
        *,
        range_ms: TimeRangeMs,
    ) -> list[FrameEvidence]: ...
    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]: ...


@runtime_checkable
class Transcriber(Protocol):
    """Async speech-to-text port."""

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse: ...


@runtime_checkable
class VisionProvider(Protocol):
    """Async multi-image visual observation port."""

    provider_name: str
    model_id: str

    async def observe(self, request: VisionRequest) -> VisionResponse: ...


@runtime_checkable
class PlannerProvider(Protocol):
    """Async bounded planning and answer synthesis port."""

    provider_name: str
    model_id: str

    async def decide(self, request: PlannerRequest) -> PlannerResponse: ...


@runtime_checkable
class VideoProvider(Protocol):
    """Reserved P1 extension port for native video models."""

    provider_name: str
    model_id: str

    async def analyze_video(
        self,
        *,
        asset_id: str,
        analysis_id: str,
        prompt_text: str,
    ) -> VisionResponse: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed managed local artifact store port."""

    def put_bytes(self, asset_id: str, kind: str, data: bytes, *, suffix: str) -> str: ...
    def read_bytes(self, artifact_id: str) -> bytes: ...
    def verify_hash(self, artifact_id: str, expected_sha256: str) -> bool: ...


@runtime_checkable
class EvidenceRepository(Protocol):
    """Persistent evidence, observation, and answer repository port."""

    def save_asset(self, asset: VideoAsset) -> None: ...
    def save_evidence(self, evidence: FrameEvidence | TranscriptSegment) -> None: ...
    def get_evidence(
        self,
        asset_id: str,
        evidence_id: str,
    ) -> FrameEvidence | TranscriptSegment | None: ...
    def save_observation(self, observation: Observation) -> None: ...
    def save_answer(self, answer: Answer) -> None: ...


@runtime_checkable
class SearchPort(Protocol):
    """Scoped Chinese bigram + English term evidence search port."""

    def search(
        self,
        asset_id: str,
        analysis_id: str,
        query: str,
        *,
        limit: int = 5,
        time_range: TimeRangeMs | None = None,
    ) -> list[SearchHit]: ...


@runtime_checkable
class BudgetStore(Protocol):
    """Budget account and usage attempt persistence port."""

    def get_or_create_account(
        self,
        scope_id: str,
        scope_kind: Literal["analysis", "qa"],
        limits: BudgetLimits,
    ) -> BudgetAccount: ...
    def save_account(self, account: BudgetAccount) -> None: ...
    def record_usage(self, record: UsageRecord, *, dispatched: bool = False) -> None:
        """Upsert an attempt and retain any previously recorded transport dispatch."""
        ...

    def list_usage(self, scope_id: str) -> list[UsageRecord]: ...

    def list_usage_with_dispatch(self, scope_id: str) -> list[tuple[UsageRecord, bool]]:
        """Return each usage row and its dispatch marker from one store snapshot."""
        ...
