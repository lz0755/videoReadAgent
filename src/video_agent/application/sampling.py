"""Explicit scene profiles, coarse window sampling and observed coverage."""

from collections.abc import Iterable
from dataclasses import dataclass
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
    raise NotImplementedError


def image_size(width: int, height: int, profile: SceneProfile) -> tuple[int, int]:
    raise NotImplementedError


def coverage_gaps(window: TimeRangeMs, actual_ms: Iterable[int]) -> list[TimeRangeMs]:
    raise NotImplementedError


class SampleInventory:
    def __init__(self, limit: int) -> None:
        raise NotImplementedError

    def admit(self, frame: FrameEvidence) -> bool:
        """Accept an already-seen time/crop, or claim one remaining unique slot."""
        raise NotImplementedError

    @property
    def count(self) -> int:
        raise NotImplementedError
