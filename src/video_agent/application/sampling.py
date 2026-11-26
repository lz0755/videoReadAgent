"""Explicit scene profiles, coarse window sampling and observed coverage."""

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from video_agent.domain.config import AnalysisConfig
from video_agent.domain.models import FrameEvidence, TimeRangeMs

type SceneProfile = Literal["software_tutorial", "overview"]


@dataclass(frozen=True)
class SampleWindow:
    core: TimeRangeMs
    context: TimeRangeMs
    requested_ms: tuple[int, ...]


@dataclass(frozen=True)
class SamplingPlan:
    profile: SceneProfile
    interval_ms: int
    windows: tuple[SampleWindow, ...]


def plan_windows(duration_ms: int, profile: SceneProfile, config: AnalysisConfig) -> SamplingPlan:
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("duration must be a positive integer in milliseconds")
    if profile not in ("software_tutorial", "overview"):
        raise ValueError("unknown scene profile")
    interval = max(1, round(Fraction(1000) / Fraction(str(config.base_fps))))
    grid = range(0, min(duration_ms, interval * config.max_unique_sampled_frames), interval)
    windows = []
    for start in range(0, duration_ms, config.core_window_ms):
        end = min(start + config.core_window_ms, duration_ms)
        context = TimeRangeMs(
            start_ms=max(0, start - config.context_overlap_ms),
            end_ms=min(duration_ms, end + config.context_overlap_ms),
        )
        times = grid[
            (context.start_ms + interval - 1) // interval : (context.end_ms + interval - 1)
            // interval
        ]
        windows.append(SampleWindow(TimeRangeMs(start_ms=start, end_ms=end), context, tuple(times)))
    return SamplingPlan(profile, interval, tuple(windows))


def image_size(width: int, height: int, profile: SceneProfile) -> tuple[int, int]:
    if min(width, height) <= 0:
        raise ValueError("image dimensions must be positive")
    if profile == "software_tutorial":
        scale = min(1.0, 1920 / max(width, height), 1080 / min(width, height))
    elif profile == "overview":
        scale = min(1.0, 960 / max(width, height))
    else:
        raise ValueError("unknown scene profile")
    return max(1, round(width * scale)), max(1, round(height * scale))


def coverage_gaps(window: TimeRangeMs, actual_ms: Iterable[int]) -> list[TimeRangeMs]:
    gaps = []
    cursor = window.start_ms
    for point in sorted(set(actual_ms)):
        if window.start_ms <= point < window.end_ms:
            if cursor < point:
                gaps.append(TimeRangeMs(start_ms=cursor, end_ms=point))
            cursor = point + 1
    if cursor < window.end_ms:
        gaps.append(TimeRangeMs(start_ms=cursor, end_ms=window.end_ms))
    return gaps


class SampleInventory:
    def __init__(self, limit: int) -> None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("unique sample limit must be a positive integer")
        self._limit = limit
        self._seen: set[tuple[int, tuple[int, int, int, int] | None]] = set()

    def admit(self, frame: FrameEvidence) -> bool:
        """Accept an already-seen time/crop, or claim one remaining unique slot."""
        crop = frame.crop
        geometry = (
            (crop.x, crop.y, crop.width, crop.height) if crop and crop.region_id != "full" else None
        )
        key = frame.actual_pts_ms, geometry
        if key not in self._seen and len(self._seen) >= self._limit:
            return False
        self._seen.add(key)
        return True

    @property
    def count(self) -> int:
        return len(self._seen)
