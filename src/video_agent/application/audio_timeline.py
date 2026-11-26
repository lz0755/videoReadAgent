"""Map extracted PCM times back to the canonical video timeline."""

from video_agent.domain.models import AudioTimelineMap, TimeRangeMs


def map_audio_range(
    timeline: AudioTimelineMap,
    local_range: TimeRangeMs,
    *,
    duration_ms: int | None = None,
) -> TimeRangeMs | None:
    """Intersect the mapped segment with the video. Empty intersections give no evidence.

    An unpadded WAV starts at the measured audio offset plus the amount trimmed
    from its start. A padded WAV already starts on the requested video timeline.
    """
    origin_ms = timeline.extracted_start_ms
    if not timeline.leading_silence_padded:
        origin_ms += timeline.audio_offset_ms
    start = max(0, origin_ms + local_range.start_ms)
    end = origin_ms + local_range.end_ms
    if duration_ms is not None:
        if type(duration_ms) is not int or duration_ms <= 0:
            raise ValueError("duration_ms must be a positive integer")
        end = min(end, duration_ms)
    return TimeRangeMs(start_ms=start, end_ms=end) if start < end else None
