from typing import cast

import pytest

from video_agent.application.sampling import coverage_gaps
from video_agent.domain.models import TimeRangeMs


@pytest.mark.parametrize("point", [False, 0.0])
def test_invalid_point_cannot_close_a_gap(point: object) -> None:
    with pytest.raises(ValueError):
        coverage_gaps(TimeRangeMs(start_ms=0, end_ms=2), [0, cast(int, point)])
