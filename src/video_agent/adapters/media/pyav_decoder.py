"""Streaming PyAV frame selection for registered media."""

from __future__ import annotations

from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.models import FrameEvidence, TimeRangeMs, VideoAsset


def extract_frames(
    store: LocalArtifactStore,
    asset: VideoAsset,
    requested_times_ms: list[int],
    *,
    range_ms: TimeRangeMs,
    max_long_edge_px: int | None = None,
) -> list[FrameEvidence]:
    raise NotImplementedError
