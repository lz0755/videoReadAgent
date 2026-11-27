"""Review real frames through one server-bound QA request."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import FrameSelection, ToolResult
from video_agent.application.observations import WindowResult
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.domain.models import FrameEvidence, Question, RegionId, VideoAsset
from video_agent.domain.ports import ImageInput, MediaBackend, VisionRequest, VisionResponse


@dataclass
class FrameReview:
    asset: VideoAsset
    question: Question
    job_id: str
    media: MediaBackend
    prompts: PromptRegistry
    prepare: Callable[[FrameEvidence, RegionId], tuple[FrameEvidence, ImageInput, str]]
    send: Callable[[VisionRequest, str], Awaitable[VisionResponse]]
    save: Callable[[WindowResult], None]
    remaining: Callable[[], float]

    async def run(self, selection: FrameSelection, parent: str) -> ToolResult:
        raise NotImplementedError
