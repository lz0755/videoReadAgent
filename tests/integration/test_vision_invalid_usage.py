from pathlib import Path

import pytest

from tests.integration.test_budget_ledger import guard_for, ledger_db, vision
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.adapters.llm.vision import StrictVisionProvider
from video_agent.domain.errors import ProviderError
from video_agent.domain.ports import VisionRequest, VisionResponse

__all__ = ["ledger_db"]


@pytest.mark.parametrize("missing", [True, False])
def test_invalid_candidate_records_separate_receipts_and_usage(
    ledger_db: Path, missing: bool
) -> None:
    class InvalidWire(FakeVisionProvider):
        async def observe(self, request: VisionRequest) -> VisionResponse:
            res = await super().observe(request)
            return res.model_copy(
                update={
                    "raw_text": '{"observations":[{}]}',
                    "cost_status": "unknown" if missing else "estimated",
                    "input_tokens": 0 if missing else 987,
                    "output_tokens": 0 if missing else 14,
                }
            )

    guard = guard_for(ledger_db)
    guard.ensure_started("a", "analysis")
    port = InvalidWire()
    port.model_id = "deepseek-flash"
    with pytest.raises(ProviderError):
        _run_coro(
            ProviderExecutor(guard, authorize=lambda: None).observe(
                StrictVisionProvider(port), vision(), scope_id="a", operation_key="invalid"
            )
        )
    records = guard.get_ledger("a").records
    assert len(records) == len(port.calls) == 2
    assert len({r.provider_request_id for r in records}) == 2 and all(
        r.provider_request_id for r in records
    )
    assert all(
        r.status == "failed" and r.cost_status == ("unknown" if missing else "estimated")
        for r in records
    )
    if missing:
        assert all(r.confirmed_cost_usd is None and r.estimated_cost_usd > 0 for r in records)
    else:
        assert all((r.input_tokens, r.output_tokens) == (987, 14) for r in records)
