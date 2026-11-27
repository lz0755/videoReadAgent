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
    if (
        mapping.status != "validated_offset"
        or mapping.offset_ms is None
        or mapping.asset_id != asset_id
        or mapping.artifact_id != artifact_id
        or not authenticated_source
        or type(actual_ms) is not int
        or actual_ms < 0
    ):
        return None
    mapped = actual_ms + mapping.offset_ms
    return mapped / 1000 if mapped >= 0 else None
