from unittest.mock import AsyncMock, Mock

import pytest

from tests.integration.test_budget_ledger import vision
from tests.security.test_agent_tools import QA, service
from tests.unit.test_budget_guard import _make_guard, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.agent.executor import ToolExecutor
from video_agent.application.evidence_service import FrameSelection, ToolResult
from video_agent.domain.errors import BudgetError, ErrorCode
from video_agent.domain.ports import ToolCallProposal


def test_each_tool_debits_before_its_own_execution() -> None:
    guard, _, _ = _make_guard()
    guard.ensure_started("Q", "qa")
    for _ in range(4):
        guard.record_tool_execution("Q", tool_name="search_evidence")
    bound = service()
    assert isinstance(bound.search, Mock)
    bound.search.search.return_value = []
    executor = ToolExecutor(guard, bound, QA)
    call = ToolCallProposal(
        tool_call_id="untrusted", tool_name="search_evidence", arguments={"query": "query"}
    )
    for _ in range(2):
        _run_coro(executor.execute(call))
    with pytest.raises(BudgetError) as error:
        _run_coro(executor.execute(call))
    assert error.value.code == ErrorCode.BUDGET_EXCEEDED
    assert bound.search.search.call_count == 2
    assert guard.get_account("Q").consumed.tool_calls == 6


def test_nested_retry_links_actual_sends_to_one_server_tool_and_compare_shares_cap() -> None:
    guard, _, _ = _make_guard()
    guard.ensure_started("Q", "qa")
    bound = service()
    provider = FakeVisionProvider(scenarios=["rate_limit", "normal"])
    transport = ProviderExecutor(guard, authorize=lambda: None)

    async def review(selection: FrameSelection, parent: str) -> ToolResult:
        req = vision().model_copy(update={"asset_id": "A", "analysis_id": "X"})
        assert len(req.images) == len(selection.points)
        await transport.observe(
            provider, req, scope_id="Q", operation_key=parent, parent_tool_call_id=parent
        )
        return ToolResult()

    bound.review_frames = review
    executor = ToolExecutor(guard, bound, QA)
    call = ToolCallProposal(
        tool_call_id="from-model",
        tool_name="inspect_clip",
        arguments={"start_ms": 0, "end_ms": 4000},
    )
    _run_coro(executor.execute(call))
    counts = guard.get_account("Q").consumed
    assert (
        counts.tool_calls,
        counts.expensive_tool_calls,
        counts.provider_attempts,
        counts.submitted_image_parts,
    ) == (1, 1, 2, 16)
    parents = {r.parent_tool_call_id for r in guard.get_ledger("Q").records}
    assert len(parents) == 1 and None not in parents and "from-model" not in parents
    bound.review_frames = AsyncMock(return_value=ToolResult())
    compare = ToolCallProposal(
        tool_call_id="pair",
        tool_name="compare_frames",
        arguments={"first_ms": 3, "second_ms": 9000},
    )
    _run_coro(executor.execute(compare))
    with pytest.raises(BudgetError):
        _run_coro(executor.execute(call))
    assert bound.review_frames.await_count == 1
