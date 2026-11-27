"""Parse proposals from the text planner at the server authority boundary."""

from video_agent.application.evidence_service import EvidenceService, FrameSelection, SearchArgs
from video_agent.domain.config import QaConfig


def validate_tool(
    name: str, arguments: dict[str, object], service: EvidenceService, config: QaConfig
) -> SearchArgs | FrameSelection:
    raise NotImplementedError
