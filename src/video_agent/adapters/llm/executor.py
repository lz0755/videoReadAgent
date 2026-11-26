"""Single provider send and settlement boundary for persistent jobs."""

from collections.abc import Callable

from video_agent.application.budget import BudgetGuard
from video_agent.domain.ports import (
    PlannerProvider,
    PlannerRequest,
    PlannerResponse,
    VisionProvider,
    VisionRequest,
    VisionResponse,
)


class ProviderExecutor:
    def __init__(self, guard: BudgetGuard, *, authorize: Callable[[], None]) -> None:
        raise NotImplementedError

    async def observe(
        self, provider: VisionProvider, request: VisionRequest, *, scope_id: str,
        operation_key: str, explicit_retry: bool = False, parent_tool_call_id: str | None = None,
    ) -> VisionResponse:
        raise NotImplementedError

    async def decide(
        self, provider: PlannerProvider, request: PlannerRequest, *, operation_key: str,
        explicit_retry: bool = False, final: bool = False,
    ) -> PlannerResponse:
        raise NotImplementedError
