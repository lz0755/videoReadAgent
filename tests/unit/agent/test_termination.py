from unittest.mock import Mock

import pytest

from tests.unit.agent.test_controller import context, harness
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.fake import FakePlannerProvider
from video_agent.application.evidence_service import ToolResult
from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import Answer, RequestBudget
from video_agent.domain.ports import PlannerRequest, PlannerResponse


def test_final_attempt_is_reserved_even_with_rounds_left() -> None:
    planner = FakePlannerProvider(mode="call_tools")
    controller, guard, _, flags = harness(planner)
    for _ in range(9):
        res = guard.reserve_provider_attempt(
            RequestBudget(
                scope_id="Q",
                job_id="J",
                provider="fake",
                model_id="fake-planner",
                image_count=0,
                estimated_cost_usd=0.0,
            )
        )
        guard.settle_success(res, input_tokens=1, output_tokens=1, confirmed_cost_usd=0.0)
    result = _run_coro(controller.run("J", ToolResult()))
    assert flags == [True] and guard.get_ledger("Q").total_attempts == 10
    assert result.gaps and guard.get_account("Q").consumed.tool_calls == 0


def test_cancellation_in_first_tool_stops_rest_of_batch_and_next_planner() -> None:
    class Two(FakePlannerProvider):
        async def decide(self, req: PlannerRequest) -> PlannerResponse:
            response = await super().decide(req)
            return response.model_copy(update={"tool_calls": response.tool_calls * 2})

    planner = Two(mode="call_tools")
    controller, guard, _, flags = harness(planner)
    port = controller.executor.service.search
    assert isinstance(port, Mock)

    def cancel(*args: object, **kwargs: object) -> list[object]:
        guard.cancel("Q")
        return []

    port.search.side_effect = cancel
    result = _run_coro(controller.run("J", ToolResult()))
    assert result.gaps and flags == [False] and port.search.call_count == 1
    assert guard.get_account("Q").consumed.tool_calls == 1


@pytest.mark.parametrize("reason", ["deadline", "cancel", "unknown", "pending", "exhausted"])
def test_terminal_budget_state_does_not_start_planner(reason: str) -> None:
    planner = FakePlannerProvider()
    controller, guard, clock, _ = harness(planner)
    if reason == "deadline":
        clock.advance(180)
    elif reason == "cancel":
        guard.cancel("Q")
    else:
        for _ in range(10 if reason == "exhausted" else 1):
            res = guard.reserve_provider_attempt(
                RequestBudget(
                    scope_id="Q",
                    job_id="J",
                    provider="fake",
                    model_id="fake-planner",
                    image_count=0,
                    estimated_cost_usd=0.0,
                    is_final_synthesis=True,
                )
            )
            if reason == "unknown":
                guard.settle_unknown(res)
            elif reason == "exhausted":
                guard.settle_success(res, input_tokens=1, output_tokens=1, confirmed_cost_usd=0.0)
    answer = _run_coro(controller.run("J", context()))
    assert answer.status == "not_answerable" and answer.gaps
    assert planner.calls == []


def test_active_unknown_cannot_send_a_final_retry() -> None:
    planner = FakePlannerProvider(scenarios=["unknown_result", "normal"])
    controller, guard, _, _ = harness(planner)
    answer = _run_coro(controller.run("J", context()))
    assert answer.status == "not_answerable" and answer.gaps
    assert len(planner.calls) == 1 and guard.get_ledger("Q").records[0].status == "unknown"


def test_only_validated_interim_claims_survive_deadline() -> None:
    class Interim(FakePlannerProvider):
        async def decide(self, req: PlannerRequest) -> PlannerResponse:
            result = await super().decide(req)
            clock.advance(181)
            return result.model_copy(update={"action": "call_tools"})

    planner = Interim()
    verified = []

    def validate(answer: Answer, data: ToolResult) -> None:
        if answer.claims[0].evidence_ids != data.observations[0].evidence_ids:
            raise DomainValidationError(ErrorCode.INVALID_CITATION, "foreign citation")
        verified.append(answer)

    ctrl, guard, clock, _ = harness(planner, validate)
    result = _run_coro(ctrl.run("J", context()))
    assert verified and result.claims == verified[0].claims and result.status == "partial"
    assert (
        result.gaps and len(planner.calls) == 1 and guard.get_account("Q").consumed.tool_calls == 0
    )
