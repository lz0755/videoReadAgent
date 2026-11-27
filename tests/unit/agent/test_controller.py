from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

from tests.security.test_agent_tools import QA, service
from tests.unit.test_budget_guard import FakeClock, _make_guard, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakePlannerProvider
from video_agent.agent.controller import Controller
from video_agent.agent.executor import ToolExecutor
from video_agent.application.budget import BudgetGuard
from video_agent.application.evidence_service import ToolResult
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.domain.models import Answer, Observation, TimeRangeMs
from video_agent.domain.ports import PlannerRequest, PlannerResponse


def context() -> ToolResult:
    return ToolResult(
        observations=[
            Observation(
                observation_id="obs",
                asset_id="A",
                analysis_id="X",
                kind="observed",
                text="修改孔径为 20 mm",
                time_range=TimeRangeMs(start_ms=15, end_ms=16),
                evidence_ids=["frame"],
            )
        ]
    )


def harness(
    provider: FakePlannerProvider,
    validate: Callable[[Answer, ToolResult], None] = lambda a, r: None,
) -> tuple[Controller, BudgetGuard, FakeClock, list[bool]]:
    guard, _, clock = _make_guard()
    guard.ensure_started("Q", "qa")
    service_ = service()
    assert isinstance(service_.search, Mock)
    service_.search.search.return_value = []
    dispatch = ProviderExecutor(guard, authorize=lambda: None)
    flags: list[bool] = []

    async def decide(req: PlannerRequest, final: bool) -> PlannerResponse:
        flags.append(final)
        return await dispatch.decide(
            provider, req, operation_key=f"round-{len(flags)}", final=final
        )

    return (
        Controller(
            ToolExecutor(guard, service_, QA),
            PromptRegistry(Path("prompts/registry.json")),
            decide,
            validate,
        ),
        guard,
        clock,
        flags,
    )


def test_text_planner_receives_only_visible_context_and_validates_before_return() -> None:
    planner = FakePlannerProvider()
    validator = Mock()
    controller, guard, _, flags = harness(planner, validator)
    answer = _run_coro(controller.run("J", context()))
    assert answer.status == "answered" and answer.claims[0].evidence_ids == ["frame"]
    validator.assert_called()
    req = planner.calls[0]
    assert req.available_evidence_ids == ["frame"] and "修改孔径为 20 mm" in req.prompt_text
    assert "artifact_id" not in req.prompt_text and not flags[0]
    assert guard.get_ledger("Q").total_attempts == 1


def test_last_round_reserved_for_final_only_even_if_planner_requests_tools() -> None:
    planner = FakePlannerProvider(mode="call_tools")
    controller, guard, _, flags = harness(planner)
    answer = _run_coro(controller.run("J", ToolResult()))
    assert flags == [False] * 4 + [True]
    assert answer.status == "not_answerable" and answer.gaps
    counts = guard.get_account("Q").consumed
    assert (counts.rounds, counts.tool_calls, counts.provider_attempts) == (5, 4, 5)
