"""Map extracted PCM times back to the canonical video timeline."""

from video_agent.domain.models import AudioTimelineMap, TimeRangeMs


def map_audio_range(
    timeline: AudioTimelineMap,
    local_range: TimeRangeMs,
    *,
    duration_ms: int | None = None,
) -> TimeRangeMs | None:
    raise NotImplementedError
