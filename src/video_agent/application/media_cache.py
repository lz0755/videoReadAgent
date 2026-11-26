"""Content-keyed reuse of verified media and ASR outputs."""

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from pydantic import ValidationError

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import (
    AudioTimelineMap,
    FrameEvidence,
    Sha256Hex,
    StrictModel,
    TimeRangeMs,
    VideoAsset,
)
from video_agent.domain.ports import (
    MediaBackend,
    Transcriber,
    TranscribeRequest,
    TranscribeResponse,
)


class MediaCacheStore(Protocol):
    def read(self, key: str) -> bytes | None: ...
    def write(self, key: str, payload: bytes) -> None: ...
    def digest(self, artifact_id: str, *, asset_id: str | None = None) -> str: ...
    def bind(
        self,
        artifact_id: str,
        sha256: str,
        *,
        from_asset: str,
        to_asset: str,
        kind: Literal["frame", "audio"],
    ) -> str | None: ...


def _key(namespace: str, *parts: object) -> str:
    raw = json.dumps(
        ["media-cache-v1", namespace, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _FrameProduct(StrictModel):
    frame: FrameEvidence
    sha256: Sha256Hex


class _Frames(StrictModel):
    products: list[_FrameProduct]


class _Audio(StrictModel):
    asset_id: str
    artifact_id: str
    sha256: Sha256Hex
    timeline: AudioTimelineMap


def _load[T: StrictModel](cache: MediaCacheStore, key: str, model: type[T]) -> T | None:
    payload = cache.read(key)
    if payload is None:
        return None
    try:
        return model.model_validate_json(payload)
    except ValidationError:
        return None


def _save(cache: MediaCacheStore, key: str, record: StrictModel) -> None:
    cache.write(key, record.model_dump_json().encode("utf-8"))


class CachedMediaBackend:
    def __init__(
        self,
        backend: MediaBackend,
        cache: MediaCacheStore,
        *,
        frame_profile: Mapping[str, object],
        audio_profile: Mapping[str, object],
    ) -> None:
        self._backend = backend
        self._cache = cache
        self._frame_profile = dict(frame_profile)
        self._audio_profile = dict(audio_profile)

    def probe_video(self, source_path: str, *, asset_id: str) -> VideoAsset:
        return self._backend.probe_video(source_path, asset_id=asset_id)

    def _source(self, asset: VideoAsset) -> str:
        if asset.status != "ready" or not asset.source_artifact_id:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "cache requires a registered source")
        if self._cache.digest(asset.source_artifact_id, asset_id=asset.asset_id) != asset.sha256:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "registered source checksum changed")
        return asset.sha256

    def extract_frames(
        self, asset: VideoAsset, requested_times_ms: list[int], *, range_ms: TimeRangeMs
    ) -> list[FrameEvidence]:
        # The immutable source content fixes intrinsic stream PTS, origin and display matrix.
        key = _key(
            "frames",
            self._source(asset),
            asset.duration_ms,
            self._frame_profile,
            range_ms.model_dump(),
            requested_times_ms,
        )
        hit = _load(self._cache, key, _Frames)
        if hit is not None and len(hit.products) == len(requested_times_ms):
            frames = []
            for i, product in enumerate(hit.products):
                frame = product.frame
                bound = self._cache.bind(
                    frame.artifact_id,
                    product.sha256,
                    from_asset=frame.asset_id,
                    to_asset=asset.asset_id,
                    kind="frame",
                )
                if bound is None:
                    break
                frames.append(
                    frame.model_copy(
                        update={
                            "asset_id": asset.asset_id,
                            "artifact_id": bound,
                            "evidence_id": "ev_frame_" + _key(key, asset.asset_id, i),
                        }
                    )
                )
            else:
                return frames
        decoded = self._backend.extract_frames(asset, requested_times_ms, range_ms=range_ms)
        if len(decoded) != len(requested_times_ms):
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "decoder returned an incomplete frame batch")
        products = []
        checksums: dict[str, str] = {}
        for i, frame in enumerate(decoded):
            if frame.asset_id != asset.asset_id:
                raise MediaError(
                    ErrorCode.CROSS_ASSET_REFERENCE, "decoder returned a foreign frame"
                )
            if frame.artifact_id not in checksums:
                checksums[frame.artifact_id] = self._cache.digest(
                    frame.artifact_id, asset_id=asset.asset_id
                )
            annotated = frame.model_copy(
                update={"evidence_id": "ev_frame_" + _key(key, asset.asset_id, i)}
            )
            products.append(_FrameProduct(frame=annotated, sha256=checksums[frame.artifact_id]))
        _save(self._cache, key, _Frames(products=products))
        return [product.frame for product in products]

    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        key = _key("audio", self._source(asset), asset.duration_ms, self._audio_profile)
        hit = _load(self._cache, key, _Audio)
        if hit is not None:
            bound = self._cache.bind(
                hit.artifact_id,
                hit.sha256,
                from_asset=hit.asset_id,
                to_asset=asset.asset_id,
                kind="audio",
            )
            if bound is not None:
                return bound, hit.timeline
        artifact_id, timeline = self._backend.extract_audio(asset)
        _save(
            self._cache,
            key,
            _Audio(
                asset_id=asset.asset_id,
                artifact_id=artifact_id,
                sha256=self._cache.digest(artifact_id, asset_id=asset.asset_id),
                timeline=timeline,
            ),
        )
        return artifact_id, timeline


class CachedTranscriber:
    def __init__(
        self,
        transcriber: Transcriber,
        cache: MediaCacheStore,
        *,
        profile: Callable[[], Mapping[str, object]],
    ) -> None:
        self._transcriber = transcriber
        self._cache = cache
        self._profile = profile

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        key = _key(
            "asr",
            self._cache.digest(request.audio_artifact_id, asset_id=request.asset_id),
            dict(self._profile()),
            request.language,
            request.vad_filter,
            request.audio_timeline.model_dump(),
        )
        response = _load(self._cache, key, TranscribeResponse)
        if response is None:
            response = await self._transcriber.transcribe(request)
            if any(
                s.asset_id != request.asset_id or s.audio_timeline != request.audio_timeline
                for s in response.segments
            ):
                raise MediaError(ErrorCode.CROSS_ASSET_REFERENCE, "ASR returned unrelated evidence")
            _save(self._cache, key, response)
        segments = [
            segment.model_copy(
                update={
                    "asset_id": request.asset_id,
                    "evidence_id": "ev_asr_" + _key(key, request.asset_id, i),
                }
            )
            for i, segment in enumerate(response.segments)
        ]
        return response.model_copy(update={"segments": segments})
