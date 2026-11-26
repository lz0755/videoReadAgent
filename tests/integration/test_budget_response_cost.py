from pathlib import Path

from tests.integration.test_budget_ledger import guard_for, ledger_db, vision
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.domain.ports import VisionRequest, VisionResponse

__all__ = ["ledger_db"]


def test_received_response_with_missing_usage_preserves_provider_result(ledger_db: Path) -> None:
    class MissingUsage(FakeVisionProvider):
        async def observe(self, request: VisionRequest) -> VisionResponse:
            result = await super().observe(request)
            return result.model_copy(
                update={"cost_status": "unknown", "input_tokens": 0, "output_tokens": 0}
            )

    fake = MissingUsage()
    fake.model_id = "deepseek-flash"
    guard = guard_for(ledger_db)
    guard.ensure_started("a", "analysis")
    response = _run_coro(
        ProviderExecutor(guard, authorize=lambda: None).observe(
            fake, vision(), scope_id="a", operation_key="one"
        )
    )
    record = guard.get_ledger("a").records[0]
    assert (
        record.status == "succeeded" and record.provider_request_id == response.provider_request_id
    )
    assert record.cost_status == "unknown" and record.confirmed_cost_usd is None
    assert record.estimated_cost_usd > 0 and not guard.get_account("a").has_unknown_external_result
