"""A time quantity can describe an action's duration or its location in the clip."""

import pytest

from video_agent.application.slice import SliceSearchIndex
from video_agent.domain.models import Observation, TimeRangeMs


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Does she wait around 80 seconds?", {"opening", "late"}),
        ("任务是否在80秒内完成？", {"opening", "late"}),
        ("What does she wait for at 80 seconds?", {"late"}),
        ("在80秒时，任务完成了吗？", {"late"}),
    ],
)
def test_duration_context_preserves_lexical_hits(query: str, expected: set[str]) -> None:
    index = SliceSearchIndex()
    index.index_observations(
        asset_id="example",
        analysis_id="trial",
        observations=[
            Observation(
                observation_id=label,
                asset_id="example",
                analysis_id="trial",
                kind="observed",
                text="She signals to wait. 任务完成。",
                time_range=TimeRangeMs(start_ms=start, end_ms=start + 1),
                evidence_ids=[label],
            )
            for label, start in (("opening", 0), ("late", 80000))
        ],
    )
    assert {hit.source_id for hit in index.search("example", "trial", query)} == expected
