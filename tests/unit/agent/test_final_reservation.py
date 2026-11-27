from tests.integration.test_budget_ledger import vision
from tests.unit.agent.test_controller import context, harness
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.application.evidence_service import FrameSelection, ToolResult
from video_agent.domain.models import RequestBudget
from video_agent.domain.ports import PlannerRequest, PlannerResponse, ToolCallProposal


def test_denied_nested_attempt_preserves_the_last_available_synthesis() -> None:
    class InspectThenAnswer(FakePlannerProvider):
        async def decide(self, req: PlannerRequest) -> PlannerResponse:
            response = await super().decide(req)
            if len(self.calls) == 1:
                return response.model_copy(
                    update={
                        "answer": None,
                        "action": "call_tools",
                        "tool_calls": [
                            ToolCallProposal(
                                tool_call_id="call",
                                tool_name="inspect_clip",
                                arguments={"start_ms": 0, "end_ms": 4000},
                            )
                        ],
                    }
                )
            return response

    planner = InspectThenAnswer()
    controller, guard, _, flags = harness(planner)
    for _ in range(8):
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
    provider = FakeVisionProvider()
    transport = ProviderExecutor(guard, authorize=lambda: None)

    async def inspect(selection: FrameSelection, parent: str) -> ToolResult:
        await transport.observe(
            provider, vision("J"), scope_id="Q", operation_key=parent, parent_tool_call_id=parent
        )
        return ToolResult()

    controller.executor.service.review_frames = inspect
    answer = _run_coro(controller.run("J", context()))
    assert flags == [False, True] and answer.status == "answered"
    assert provider.calls == [] and guard.get_ledger("Q").total_attempts == 10
