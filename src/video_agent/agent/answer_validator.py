"""Validate final and interim citations against the bound question and visible evidence."""

from collections.abc import Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import ToolResult
from video_agent.domain.models import Answer, FrameEvidence, Question, TranscriptSegment


@dataclass
class AnswerValidator:
    question: Question
    duration_ms: int
    resolve: Callable[[str], FrameEvidence | TranscriptSegment | None]

    def validate(self, answer: Answer, context: ToolResult) -> None:
        raise NotImplementedError
