"""Content-keyed reuse of verified media and ASR outputs."""

from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from video_agent.domain.models import AudioTimelineMap, FrameEvidence, TimeRangeMs, VideoAsset
from video_agent.domain.ports import (
    MediaBackend,
    TranscribeRequest,
    TranscribeResponse,
    Transcriber,
)


class MediaCacheStore(Protocol):
    def read(self, key: str) -> bytes | None: ...
    def write(self, key: str, payload: bytes) -> None: ...
    def digest(self, artifact_id: str) -> str: ...
    def bind(
        self,
        artifact_id: str,
        sha256: str,
        *,
        from_asset: str,
        to_asset: str,
        kind: Literal["frame", "audio"],
    ) -> str | None: ...


class CachedMediaBackend:
    def __init__(
        self,
        backend: MediaBackend,
        cache: MediaCacheStore,
        *,
        frame_profile: Mapping[str, object],
        audio_profile: Mapping[str, object],
    ) -> None:
        raise NotImplementedError

    def probe_video(self, source_path: str, *, asset_id: str) -> VideoAsset:
        raise NotImplementedError

    def extract_frames(
        self, asset: VideoAsset, requested_times_ms: list[int], *, range_ms: TimeRangeMs
    ) -> list[FrameEvidence]:
        raise NotImplementedError

    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        raise NotImplementedError


class CachedTranscriber:
    def __init__(
        self,
        transcriber: Transcriber,
        cache: MediaCacheStore,
        *,
        profile: Callable[[], Mapping[str, object]],
    ) -> None:
        raise NotImplementedError

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        raise NotImplementedError
