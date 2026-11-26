from typing import cast

import pytest

from video_agent.application.sampling import (
    SampleInventory,
    SceneProfile,
    coverage_gaps,
    image_size,
    plan_windows,
)
from video_agent.bootstrap import load_app_config
from video_agent.domain.models import CropBox, FrameEvidence, RationalTime, TimeRangeMs


def test_half_open_core_context_and_global_sampling_grid() -> None:
    config = load_app_config().analysis
    plan = plan_windows(40_001, "software_tutorial", config)
    assert plan.interval_ms == 5000
    assert [
        (w.core.start_ms, w.core.end_ms, w.context.start_ms, w.context.end_ms) for w in plan.windows
    ] == [(0, 20000, 0, 22000), (20000, 40000, 18000, 40001), (40000, 40001, 38000, 40001)]
    assert plan.windows[0].requested_ms == (0, 5000, 10000, 15000, 20000)
    assert plan.windows[-1].requested_ms == (40000,)
    whole = plan_windows(600_000, "overview", config)
    assert len(whole.windows) == 30
    assert len({t for w in whole.windows for t in w.requested_ms}) == 120
    limited = plan_windows(
        600_000, "overview", config.model_copy(update={"max_unique_sampled_frames": 2})
    )
    assert {t for w in limited.windows for t in w.requested_ms} == {0, 5000}
    assert limited.windows[-1].requested_ms == ()


def test_profiles_bound_both_axes_without_upscaling() -> None:
    assert image_size(1920, 1080, "software_tutorial") == (1920, 1080)
    assert image_size(1080, 1920, "software_tutorial") == (1080, 1920)
    assert image_size(2560, 1440, "software_tutorial") == (1920, 1080)
    assert image_size(2000, 2000, "software_tutorial") == (1080, 1080)
    assert image_size(1920, 1080, "overview") == (960, 540)
    assert image_size(400, 200, "overview") == (400, 200)
    with pytest.raises(ValueError):
        plan_windows(9000, cast(SceneProfile, "automatic"), load_app_config().analysis)


def test_coverage_uses_only_actual_canonical_points() -> None:
    window = TimeRangeMs(start_ms=5000, end_ms=10001)
    assert [
        (r.start_ms, r.end_ms) for r in coverage_gaps(window, [4999, 5003, 5003, 9000, 10001])
    ] == [(5000, 5003), (5004, 9000), (9001, 10001)]
    assert coverage_gaps(window, []) == [window]


def test_inventory_counts_actual_time_and_geometry() -> None:
    tb = RationalTime(numerator=1, denominator=1000)
    frame = FrameEvidence(
        evidence_id="a",
        asset_id="asset",
        artifact_id="f",
        requested_ms=0,
        actual_pts_ms=12,
        raw_pts=12,
        time_base=tb,
        origin_pts=0,
        origin_time_base=tb,
        duration_ms=100,
    )
    inventory = SampleInventory(2)
    assert inventory.admit(frame)
    assert inventory.admit(frame.model_copy(update={"requested_ms": 10, "artifact_id": "other"}))
    crop = CropBox(region_id="r1c1", x=0, y=0, width=5, height=6)
    assert inventory.admit(frame.model_copy(update={"crop": crop}))
    assert not inventory.admit(frame.model_copy(update={"raw_pts": 13, "actual_pts_ms": 13}))
    assert inventory.count == 2
