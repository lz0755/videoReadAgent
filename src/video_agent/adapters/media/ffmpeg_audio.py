"""Supervised local FFmpeg extraction of canonical video windows."""

from collections.abc import Callable

from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.models import AudioTimelineMap, TimeRangeMs, VideoAsset


class FFmpegAudioExtractor:
    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        executable: str = "ffmpeg",
        timeout_seconds: float = 120.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        pass

    def extract_audio(
        self, asset: VideoAsset, *, range_ms: TimeRangeMs | None = None
    ) -> tuple[str, AudioTimelineMap]:
        raise NotImplementedError
