"""Server-bound evidence reads and frame review contracts."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import Field, StrictInt

from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import (
    FrameEvidence,
    Observation,
    Question,
    RegionId,
    StrictModel,
    TimeRangeMs,
)
from video_agent.domain.ports import SearchHit, SearchPort


class SearchArgs(StrictModel):
    query: str
    limit: StrictInt = Field(default=5, ge=1, le=10)
    time_range: TimeRangeMs | None = None


@dataclass(frozen=True)
class FrameSelection:
    window: TimeRangeMs
    points: tuple[int, ...]
    region: RegionId
    compare: bool = False


class ToolResult(StrictModel):
    hits: list[SearchHit] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    frames: list[FrameEvidence] = Field(default_factory=list)
    gaps: list[TimeRangeMs] = Field(default_factory=list)


@dataclass
class EvidenceService:
    question: Question
    duration_ms: int
    search: SearchPort
    read_observation: Callable[[SearchHit], Observation]
    review_frames: Callable[[FrameSelection, str], Awaitable[ToolResult]]
    allow_region: Callable[[RegionId, TimeRangeMs], bool]

    async def run(self, request: SearchArgs | FrameSelection, parent: str) -> ToolResult:
        if isinstance(request, FrameSelection):
            return await self.review_frames(request, parent)
        asset, analysis = self.question.asset_id, self.question.analysis_id
        hits = self.search.search(
            asset, analysis, request.query, limit=request.limit, time_range=request.time_range
        )
        observations = []
        for hit in hits:
            if (hit.asset_id, hit.analysis_id) != (asset, analysis):
                raise DomainValidationError(ErrorCode.INVALID_CITATION, "foreign search hit")
            obs = self.read_observation(hit)
            if (obs.asset_id, obs.analysis_id) != (asset, analysis) or set(obs.evidence_ids) != set(
                hit.evidence_ids
            ):
                raise DomainValidationError(ErrorCode.INVALID_CITATION, "unbound search result")
            observations.append(obs)
        return ToolResult(hits=hits, observations=observations)
