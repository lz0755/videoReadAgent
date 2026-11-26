from pathlib import Path

import pytest

from tests.integration.test_budget_ledger import ledger_db, vision
from tests.unit.test_budget_guard import FakeClock, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.adapters.storage.budget import SQLiteBudgetStore
from video_agent.application.budget import BudgetGuard, SliceRunLimits
from video_agent.bootstrap import load_app_config, load_price_config
from video_agent.domain.errors import BudgetError, ErrorCode

__all__ = ["ledger_db"]


@pytest.mark.parametrize(
    "cap",
    [
        "deadline_seconds",
        "max_provider_attempts",
        "max_submitted_image_parts",
        "max_estimated_cost_usd",
    ],
)
def test_reopen_cannot_expand_first_approved_limits(ledger_db: Path, cap: str) -> None:
    clock = FakeClock()
    port = FakeVisionProvider()
    port.model_id = "deepseek-flash"

    def build(limits: SliceRunLimits) -> BudgetGuard:
        return BudgetGuard(
            store=SQLiteBudgetStore(ledger_db),
            clock=clock,
            config=load_app_config(),
            prices=load_price_config(),
            run_limits=limits,
        )

    guard = build(SliceRunLimits())
    cost = guard.estimate_slice_request(port.model_id, vision().prompt_text, 8)
    limit = {
        "deadline_seconds": 5,
        "max_provider_attempts": 1,
        "max_submitted_image_parts": 8,
        "max_estimated_cost_usd": cost,
    }[cap]
    first = build(SliceRunLimits.model_validate({cap: limit}))
    first.ensure_started("a", "analysis")
    _run_coro(
        ProviderExecutor(first, authorize=lambda: None).observe(
            port, vision(), scope_id="a", operation_key="1"
        )
    )
    clock.advance(6)
    reopened = build(SliceRunLimits())
    reopened.ensure_started("a", "analysis")
    with pytest.raises(BudgetError) as err:
        _run_coro(
            ProviderExecutor(reopened, authorize=lambda: None).observe(
                port, vision(), scope_id="a", operation_key="2"
            )
        )
    assert err.value.code == (
        ErrorCode.DEADLINE_EXCEEDED if cap == "deadline_seconds" else ErrorCode.BUDGET_EXCEEDED
    )
    assert len(port.calls) == 1
