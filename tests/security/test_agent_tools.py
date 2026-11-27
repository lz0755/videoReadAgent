from unittest.mock import AsyncMock, Mock

import pytest

from tests.unit.test_budget_guard import _run_coro
from video_agent.agent.tools import validate_tool
from video_agent.application.evidence_service import EvidenceService, FrameSelection, ToolResult
from video_agent.bootstrap import load_app_config
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import Question, TimeRangeMs

QA = load_app_config().qa


def service() -> EvidenceService:
    return EvidenceService(
        Question(
            question_id="Q", asset_id="A", analysis_id="X", query_text="孔径", created_at_utc="now"
        ),
        30000,
        Mock(),
        Mock(),
        AsyncMock(return_value=ToolResult()),
        lambda region, span: region == "r1c1" and span.end_ms <= 8000,
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_evidence", {"id": "any"}),
        ("shell", {"command": "echo hello"}),
        ("inspect_clip", {"start_ms": 0, "end_ms": 8001}),
        ("inspect_clip", {"start_ms": "1", "end_ms": 2000}),
        ("inspect_clip", {"start_ms": False, "end_ms": 2000}),
        ("inspect_clip", {"start_ms": 0.5, "end_ms": 2000}),
        ("inspect_clip", {"start_ms": 0, "end_ms": 2000, "region_id": [0.2, 0.3]}),
        ("inspect_clip", {"start_ms": 8000, "end_ms": 8100, "region_id": "r1c1"}),
        ("compare_frames", {"first_ms": 30, "second_ms": 30000}),
        ("compare_frames", {"first_ms": 10, "second_ms": 10}),
        ("compare_frames", {"first_ms": 10, "second_ms": 12, "path": "local.mp4"}),
        ("search_evidence", {"query": "孔径", "asset_id": "B"}),
        ("search_evidence", {"query": "孔径", "url": "https://example.invalid"}),
        ("search_evidence", {"query": "孔径", "limit": True}),
        ("search_evidence", {"query": "孔径", "time_range": {"start_ms": 30000, "end_ms": 31000}}),
    ],
)
def test_untrusted_proposals_rejected(name: str, arguments: dict[str, object]) -> None:
    with pytest.raises(VideoAgentError):
        validate_tool(name, arguments, service(), QA)


def test_bound_search_and_full_or_selected_review() -> None:
    bound = service()
    assert isinstance(bound.search, Mock)
    bound.search.search.return_value = []
    request = validate_tool("search_evidence", {"query": "孔径"}, bound, QA)
    assert _run_coro(bound.run(request, "p")).hits == []
    bound.search.search.assert_called_once_with("A", "X", "孔径", limit=5, time_range=None)
    clip = validate_tool(
        "inspect_clip", {"start_ms": 0, "end_ms": 8000, "region_id": "r1c1"}, bound, QA
    )
    assert isinstance(clip, FrameSelection)
    assert clip.points == tuple(range(0, 8000, 500)) and clip.region == "r1c1"
    assert clip.window == TimeRangeMs(start_ms=0, end_ms=8000)
    assert _run_coro(bound.run(clip, "server-parent")) == ToolResult()
    assert isinstance(bound.review_frames, AsyncMock)
    bound.review_frames.assert_awaited_once_with(clip, "server-parent")
    compared = validate_tool("compare_frames", {"first_ms": 150, "second_ms": 29000}, bound, QA)
    assert isinstance(compared, FrameSelection) and compared.compare
    assert compared.points == (150, 29000) and compared.region == "full"
