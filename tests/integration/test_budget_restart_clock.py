from pathlib import Path

import pytest

from tests.integration.test_budget_ledger import guard_for, ledger_db, vision
from tests.unit.test_budget_guard import FakeClock, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.domain.errors import BudgetError, ErrorCode

__all__ = ["ledger_db"]


def test_reopened_scope_rejects_clock_before_last_activity(ledger_db: Path) -> None:
    clock = FakeClock()
    original = guard_for(ledger_db, clock)
    original.ensure_started("a", "analysis")
    clock.advance(120)
    fake = FakeVisionProvider()
    _run_coro(
        ProviderExecutor(original, authorize=lambda: None).observe(
            fake, vision(), scope_id="a", operation_key="one"
        )
    )
    clock.jump_wall_backwards(30)
    reopened = guard_for(ledger_db, clock)
    with pytest.raises(BudgetError) as err:
        reopened.ensure_started("a", "analysis")
        _run_coro(
            ProviderExecutor(reopened, authorize=lambda: None).observe(
                fake, vision(), scope_id="a", operation_key="two"
            )
        )
    assert err.value.code == ErrorCode.CLOCK_SKEW_DETECTED and len(fake.calls) == 1
