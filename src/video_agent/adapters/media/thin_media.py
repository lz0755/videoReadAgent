"""Thin M1.5.1 local artifact store and PyAV + FFmpeg MediaBackend adapter.

Preserves exact PTS, time_base, canonical video origin, and audio offset without
fabricating timestamps from frame_index/fps, requested_ms, or zero.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Protocol

import av

from video_agent.adapters.media.ffmpeg_audio import FFmpegAudioExtractor
from video_agent.adapters.media.pyav_decoder import extract_frames
from video_agent.domain.errors import ErrorCode, MediaError, StorageError
from video_agent.domain.models import (
    AudioTimelineMap,
    FrameEvidence,
    TimeRangeMs,
    VideoAsset,
)

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_COPY_CHUNK_BYTES = 8 * 1024 * 1024


def _sanitize_segment(raw: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("_", raw).strip("._")
    return cleaned or "item"


def _compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


class LocalArtifactStore:
    """Thin managed local filesystem ArtifactStore for M1.5 vertical slice."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._paths: dict[str, Path] = {}

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def _safe_target_dir(self, asset_id: str) -> Path:
        asset_dir = (self._root_dir / _sanitize_segment(asset_id)).resolve()
        if self._root_dir not in asset_dir.parents and asset_dir != self._root_dir:
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                f"resolved artifact directory escapes managed root for asset '{asset_id}'",
            )
        asset_dir.mkdir(parents=True, exist_ok=True)
        return asset_dir

    def put_bytes(self, asset_id: str, kind: str, data: bytes, *, suffix: str) -> str:
        safe_kind = _sanitize_segment(kind)
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        safe_suffix = "." + _sanitize_segment(safe_suffix.lstrip("."))
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"art_{safe_kind}_{digest[:12]}_{uuid.uuid4().hex[:8]}"

        asset_dir = self._safe_target_dir(asset_id)
        final_path = asset_dir / f"{artifact_id}{safe_suffix}"
        tmp_path = asset_dir / f".{artifact_id}.tmp"
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, final_path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                f"failed to persist artifact '{artifact_id}': {exc}",
            ) from exc

        self._paths[artifact_id] = final_path
        return artifact_id

    def put_file_copy(self, asset_id: str, kind: str, source_path: Path) -> tuple[str, str, int]:
        """Copy an authorized local file in 8 MiB chunks and return (artifact_id, sha256, bytes)."""
        if not source_path.is_file():
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"source media file does not exist: {source_path.name}",
            )
        safe_kind = _sanitize_segment(kind)
        safe_suffix = "." + _sanitize_segment(source_path.suffix.lstrip(".") or "bin")
        asset_dir = self._safe_target_dir(asset_id)
        tmp_id = uuid.uuid4().hex[:12]
        tmp_path = asset_dir / f".copy_{tmp_id}.tmp"

        hasher = hashlib.sha256()
        total_bytes = 0
        try:
            with source_path.open("rb") as src, tmp_path.open("wb") as dst:
                while chunk := src.read(_COPY_CHUNK_BYTES):
                    hasher.update(chunk)
                    dst.write(chunk)
                    total_bytes += len(chunk)
            if total_bytes <= 0:
                tmp_path.unlink(missing_ok=True)
                raise MediaError(
                    ErrorCode.CORRUPT_MEDIA,
                    f"source media file is empty: {source_path.name}",
                )
            digest = hasher.hexdigest()
            artifact_id = f"art_{safe_kind}_{digest[:12]}_{uuid.uuid4().hex[:8]}"
            final_path = asset_dir / f"{artifact_id}{safe_suffix}"
            os.replace(tmp_path, final_path)
        except MediaError:
            raise
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                f"failed copying source file '{source_path.name}': {exc}",
            ) from exc

        self._paths[artifact_id] = final_path
        return artifact_id, digest, total_bytes

    def resolve_path(self, artifact_id: str) -> Path:
        cached = self._paths.get(artifact_id)
        if cached is not None and cached.is_file():
            return cached
        matches = list(self._root_dir.rglob(f"{artifact_id}.*"))
        if len(matches) == 1 and matches[0].is_file():
            self._paths[artifact_id] = matches[0]
            return matches[0]
        raise StorageError(
            ErrorCode.STORAGE_ERROR,
            f"managed artifact not found: {artifact_id}",
        )

    def read_bytes(self, artifact_id: str) -> bytes:
        path = self.resolve_path(artifact_id)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                f"failed reading artifact '{artifact_id}': {exc}",
            ) from exc

    def verify_hash(self, artifact_id: str, expected_sha256: str) -> bool:
        path = self.resolve_path(artifact_id)
        return _compute_file_sha256(path) == expected_sha256.lower()


class _RationalLike(Protocol):
    @property
    def numerator(self) -> int: ...

    @property
    def denominator(self) -> int: ...


def _fraction_from_time_base(tb: _RationalLike | None) -> Fraction:
    if tb is None or tb.numerator <= 0 or tb.denominator <= 0:
        raise MediaError(
            ErrorCode.TIMESTAMP_UNRELIABLE,
            "media stream/frame is missing a valid positive rational time_base",
        )
    return Fraction(int(tb.numerator), int(tb.denominator))


class PyAVMediaBackend:
    """Thin MediaBackend using PyAV streaming decode and FFmpeg audio extraction."""

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        max_long_edge_px: int | None = None,
        register: Callable[[str, str], VideoAsset] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._max_long_edge_px = max_long_edge_px
        self._register = register
        self._audio = FFmpegAudioExtractor(artifact_store, cancelled=cancelled)

    def probe_video(self, source_path: str, *, asset_id: str) -> VideoAsset:
        if self._register is not None:
            return self._register(source_path, asset_id)
        src = Path(source_path)
        source_artifact_id, sha256, size_bytes = self._artifact_store.put_file_copy(
            asset_id,
            "source_video",
            src,
        )
        managed_path = self._artifact_store.resolve_path(source_artifact_id)

        try:
            with av.open(str(managed_path)) as container:
                video_streams = list(container.streams.video)
                audio_streams = list(container.streams.audio)
                if not video_streams:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"no video stream found in {src.name}",
                    )
                v_stream = video_streams[0]
                width = int(v_stream.codec_context.width or 0)
                height = int(v_stream.codec_context.height or 0)
                video_codec = str(v_stream.codec_context.name or "unknown")
                if width <= 0 or height <= 0:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"invalid video dimensions ({width}x{height}) in {src.name}",
                    )

                # Decode the very first valid video frame to prove decodability and reliable PTS.
                first_pts: int | None = None
                first_tb: Fraction | None = None
                for frame in container.decode(v_stream):
                    if frame.pts is None:
                        raise MediaError(
                            ErrorCode.TIMESTAMP_UNRELIABLE,
                            f"first decoded video frame has missing PTS in {src.name}",
                        )
                    first_pts = int(frame.pts)
                    first_tb = _fraction_from_time_base(frame.time_base or v_stream.time_base)
                    break

                if first_pts is None or first_tb is None:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"no decodable video frames found in {src.name}",
                    )

                duration_ms: int | None = None
                if v_stream.duration is not None and v_stream.time_base is not None:
                    stream_tb = _fraction_from_time_base(v_stream.time_base)
                    duration_ms = math.floor(Fraction(int(v_stream.duration)) * stream_tb * 1000)
                elif container.duration is not None:
                    duration_ms = math.floor(Fraction(int(container.duration), av.time_base) * 1000)

                if duration_ms is None or duration_ms <= 0:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"unable to determine positive video duration for {src.name}",
                    )

                has_audio = len(audio_streams) > 0
                audio_codec = (
                    str(audio_streams[0].codec_context.name or "unknown") if has_audio else None
                )
        except MediaError:
            raise
        except (av.FFmpegError, OSError, ValueError) as exc:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"failed to open or decode media file '{src.name}': {exc}",
            ) from exc

        return VideoAsset(
            asset_id=asset_id,
            source_artifact_id=source_artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            duration_ms=duration_ms,
            width=width,
            height=height,
            video_codec=video_codec,
            has_audio=has_audio,
            audio_codec=audio_codec,
            display_filename=src.name,
            status="ready",
        )

    def extract_frames(
        self,
        asset: VideoAsset,
        requested_times_ms: list[int],
        *,
        range_ms: TimeRangeMs,
    ) -> list[FrameEvidence]:
        return extract_frames(
            self._artifact_store,
            asset,
            requested_times_ms,
            range_ms=range_ms,
            max_long_edge_px=self._max_long_edge_px,
        )

    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        return self._audio.extract_audio(asset)
