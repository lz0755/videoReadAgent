"""Finite planning loop over an already bound QA execution."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.agent.executor import ToolExecutor
from video_agent.application.evidence_service import ToolResult
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.domain.models import Answer
from video_agent.domain.ports import PlannerRequest, PlannerResponse


@dataclass
class Controller:
    executor: ToolExecutor
    prompts: PromptRegistry
    decide: Callable[[PlannerRequest, bool], Awaitable[PlannerResponse]]
    validate: Callable[[Answer, ToolResult], None]

    async def run(self, job_id: str, initial: ToolResult) -> Answer:
        raise NotImplementedError
