"""Locate an event from a coarse index hit and two bounded visual inspections."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import ToolResult
from video_agent.domain.models import Answer, FrameEvidence, Question
from video_agent.domain.ports import SearchHit, ToolCallProposal


@dataclass
class EventLocalizer:
    question: Question
    duration_ms: int
    context: Callable[[SearchHit], list[FrameEvidence]]
    execute: Callable[[ToolCallProposal], Awaitable[ToolResult]]

    async def run(self, job_id: str, initial: ToolResult) -> tuple[Answer, ToolResult]:
        raise NotImplementedError
