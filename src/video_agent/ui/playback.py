"""Player navigation from a measured source mapping."""

from video_agent.domain.models import PlaybackTimelineMap


def playback_seconds(
    mapping: PlaybackTimelineMap,
    *,
    asset_id: str,
    artifact_id: str,
    actual_ms: int,
    authenticated_source: bool,
) -> float | None:
    raise NotImplementedError("M5 playback navigation")
