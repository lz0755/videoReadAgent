"""Thin M1.5.1 local artifact store and PyAV + FFmpeg MediaBackend adapter.

Preserves exact PTS, time_base, canonical video origin, and audio offset without
fabricating timestamps from frame_index/fps, requested_ms, or zero.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol, cast

import av
from PIL import Image

from video_agent.domain.errors import ErrorCode, MediaError, StorageError
from video_agent.domain.models import (
    AudioTimelineMap,
    FrameEvidence,
    RationalTime,
    TimeRangeMs,
    VideoAsset,
    compute_canonical_pts_ms,
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


def _encode_frame_jpeg(frame: av.VideoFrame, *, max_long_edge_px: int | None) -> bytes:
    frame_any: Any = frame
    img: Image.Image = frame_any.to_image()
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max_long_edge_px is not None:
        long_edge = max(img.width, img.height)
        if long_edge > max_long_edge_px:
            scale = max_long_edge_px / long_edge
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


class PyAVMediaBackend:
    """Thin MediaBackend using PyAV streaming decode and FFmpeg audio extraction."""

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        max_long_edge_px: int | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._max_long_edge_px = max_long_edge_px

    def probe_video(self, source_path: str, *, asset_id: str) -> VideoAsset:
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
        if asset.source_artifact_id is None:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"asset '{asset.asset_id}' has no bound source_artifact_id",
            )
        try:
            range_ms.validate_within(asset.duration_ms)
        except ValueError as exc:
            raise MediaError(ErrorCode.INVALID_TIME_RANGE, str(exc)) from exc

        if not requested_times_ms:
            return []

        for req in requested_times_ms:
            if isinstance(req, bool) or not isinstance(req, int) or req < 0:
                raise MediaError(
                    ErrorCode.INVALID_TIMESTAMP,
                    f"requested_ms must be a non-negative strict int, got {req!r}",
                )
            if req < range_ms.start_ms or req >= range_ms.end_ms:
                raise MediaError(
                    ErrorCode.NO_FRAME_IN_RANGE,
                    (
                        f"requested_ms={req} is outside half-open range "
                        f"[{range_ms.start_ms}, {range_ms.end_ms})"
                    ),
                )

        managed_path = self._artifact_store.resolve_path(asset.source_artifact_id)
        indexed_requests = sorted(enumerate(requested_times_ms), key=lambda item: item[1])
        results: list[FrameEvidence | None] = [None] * len(requested_times_ms)
        pending_cursor = 0

        try:
            with av.open(str(managed_path)) as container:
                video_streams = list(container.streams.video)
                if not video_streams:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"no video stream in managed source for '{asset.asset_id}'",
                    )
                v_stream = video_streams[0]
                origin_pts: int | None = None
                origin_tb: Fraction | None = None
                cached_artifact_by_pts: dict[int, str] = {}

                for frame in container.decode(v_stream):
                    if frame.pts is None:
                        raise MediaError(
                            ErrorCode.TIMESTAMP_UNRELIABLE,
                            f"decoded video frame missing PTS in '{asset.asset_id}'",
                        )
                    raw_pts = int(frame.pts)
                    frame_tb = _fraction_from_time_base(frame.time_base or v_stream.time_base)
                    if origin_pts is None or origin_tb is None:
                        origin_pts = raw_pts
                        origin_tb = frame_tb

                    exact_ms = (
                        Fraction(raw_pts) * frame_tb - Fraction(origin_pts) * origin_tb
                    ) * 1000
                    if exact_ms < 0:
                        raise MediaError(
                            ErrorCode.TIMESTAMP_UNRELIABLE,
                            f"non-monotonic negative canonical frame PTS in '{asset.asset_id}'",
                        )

                    while pending_cursor < len(indexed_requests):
                        orig_idx, req_ms = indexed_requests[pending_cursor]
                        if exact_ms < Fraction(req_ms, 1):
                            break

                        # First decoded frame with exact_ms >= req_ms MUST still fall inside
                        # half-open [range_ms.start_ms, range_ms.end_ms) and [0, duration_ms).
                        if exact_ms >= Fraction(range_ms.end_ms, 1) or exact_ms >= Fraction(
                            asset.duration_ms, 1
                        ):
                            raise MediaError(
                                ErrorCode.NO_FRAME_IN_RANGE,
                                (
                                    f"no frame found for requested_ms={req_ms} within "
                                    f"[{range_ms.start_ms}, {range_ms.end_ms}); next frame "
                                    f"is at {float(exact_ms):.2f}ms"
                                ),
                            )

                        try:
                            actual_pts_ms = compute_canonical_pts_ms(
                                raw_pts=raw_pts,
                                time_base_num=frame_tb.numerator,
                                time_base_den=frame_tb.denominator,
                                origin_pts=origin_pts,
                                origin_time_base_num=origin_tb.numerator,
                                origin_time_base_den=origin_tb.denominator,
                                duration_ms=asset.duration_ms,
                                range_ms=range_ms,
                            )
                        except ValueError as exc:
                            code = (
                                ErrorCode.NO_FRAME_IN_RANGE
                                if ErrorCode.NO_FRAME_IN_RANGE.value in str(exc)
                                else ErrorCode.INVALID_TIMESTAMP
                            )
                            raise MediaError(code, str(exc)) from exc

                        artifact_id = cached_artifact_by_pts.get(raw_pts)
                        if artifact_id is None:
                            jpeg_bytes = _encode_frame_jpeg(
                                frame, max_long_edge_px=self._max_long_edge_px
                            )
                            artifact_id = self._artifact_store.put_bytes(
                                asset.asset_id,
                                "frame",
                                jpeg_bytes,
                                suffix=".jpg",
                            )
                            cached_artifact_by_pts[raw_pts] = artifact_id

                        evidence_id = f"ev_frame_{orig_idx + 1:04d}_{actual_pts_ms}ms"
                        results[orig_idx] = FrameEvidence(
                            evidence_id=evidence_id,
                            asset_id=asset.asset_id,
                            artifact_id=artifact_id,
                            requested_ms=req_ms,
                            actual_pts_ms=actual_pts_ms,
                            duration_ms=asset.duration_ms,
                            raw_pts=raw_pts,
                            time_base=RationalTime(
                                numerator=frame_tb.numerator,
                                denominator=frame_tb.denominator,
                            ),
                            origin_pts=origin_pts,
                            origin_time_base=RationalTime(
                                numerator=origin_tb.numerator,
                                denominator=origin_tb.denominator,
                            ),
                        )
                        pending_cursor += 1

                    if pending_cursor >= len(indexed_requests):
                        break
                    if exact_ms >= Fraction(range_ms.end_ms, 1):
                        break
        except MediaError:
            raise
        except (av.FFmpegError, OSError, ValueError) as exc:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"frame extraction failed for '{asset.asset_id}': {exc}",
            ) from exc

        if pending_cursor < len(indexed_requests):
            _, missing_req = indexed_requests[pending_cursor]
            raise MediaError(
                ErrorCode.NO_FRAME_IN_RANGE,
                (
                    f"stream ended before finding frame for requested_ms={missing_req} "
                    f"within [{range_ms.start_ms}, {range_ms.end_ms})"
                ),
            )

        return [cast(FrameEvidence, item) for item in results]

    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        if not asset.has_audio:
            raise MediaError(
                ErrorCode.NO_AUDIO_STREAM,
                f"asset '{asset.asset_id}' does not contain an audio stream",
            )
        if asset.source_artifact_id is None:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"asset '{asset.asset_id}' has no bound source_artifact_id",
            )

        managed_path = self._artifact_store.resolve_path(asset.source_artifact_id)
        try:
            with av.open(str(managed_path)) as container:
                video_streams = list(container.streams.video)
                audio_streams = list(container.streams.audio)
                if not audio_streams:
                    raise MediaError(
                        ErrorCode.NO_AUDIO_STREAM,
                        f"container for '{asset.asset_id}' has no audio stream",
                    )
                if not video_streams:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"container for '{asset.asset_id}' has no video stream",
                    )

                v_stream = video_streams[0]
                origin_pts: int | None = None
                origin_tb: Fraction | None = None
                for v_frame in container.decode(v_stream):
                    if v_frame.pts is None:
                        raise MediaError(
                            ErrorCode.TIMESTAMP_UNRELIABLE,
                            f"video origin frame missing PTS in '{asset.asset_id}'",
                        )
                    origin_pts = int(v_frame.pts)
                    origin_tb = _fraction_from_time_base(v_frame.time_base or v_stream.time_base)
                    break

                if origin_pts is None or origin_tb is None:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"no decodable video frame to establish origin in '{asset.asset_id}'",
                    )

                container.seek(0)
                a_stream = audio_streams[0]
                first_audio_pts: int | None = None
                first_audio_tb: Fraction | None = None
                for a_frame in container.decode(a_stream):
                    if a_frame.pts is None:
                        continue
                    first_audio_pts = int(a_frame.pts)
                    first_audio_tb = _fraction_from_time_base(
                        a_frame.time_base or a_stream.time_base
                    )
                    break

                if first_audio_pts is None or first_audio_tb is None:
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA,
                        f"no decodable audio frames with PTS in '{asset.asset_id}'",
                    )

                audio_offset_exact_ms = (
                    Fraction(first_audio_pts) * first_audio_tb - Fraction(origin_pts) * origin_tb
                ) * 1000
                audio_offset_ms = math.floor(audio_offset_exact_ms)
        except MediaError:
            raise
        except (av.FFmpegError, OSError, ValueError) as exc:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"failed reading audio timeline for '{asset.asset_id}': {exc}",
            ) from exc

        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin is None:
            raise MediaError(
                ErrorCode.DEPENDENCY_MISSING,
                "ffmpeg executable not found on PATH",
            )

        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(managed_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "pipe:1",
            ],
            check=False,
            capture_output=True,
            close_fds=True,
            timeout=120,
        )
        if proc.returncode != 0 or len(proc.stdout) < 44 or proc.stdout[:4] != b"RIFF":
            stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"ffmpeg audio extraction failed (exit={proc.returncode}): {stderr_msg}",
            )

        audio_artifact_id = self._artifact_store.put_bytes(
            asset.asset_id,
            "audio",
            proc.stdout,
            suffix=".wav",
        )
        audio_timeline = AudioTimelineMap(
            origin_pts=origin_pts,
            origin_time_base=RationalTime(
                numerator=origin_tb.numerator,
                denominator=origin_tb.denominator,
            ),
            audio_offset_ms=audio_offset_ms,
            extracted_start_ms=0,
            leading_silence_padded=False,
            vad_filter=False,
        )
        return audio_artifact_id, audio_timeline
