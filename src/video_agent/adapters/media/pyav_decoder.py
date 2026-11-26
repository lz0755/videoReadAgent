"""Streaming PyAV frame selection for registered media."""

from __future__ import annotations

import io
import math
import struct
from collections.abc import Generator, Iterator
from contextlib import ExitStack
from fractions import Fraction
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import av
from av.sidedata.sidedata import Type
from PIL import Image

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import FrameEvidence, RationalTime, TimeRangeMs, VideoAsset

if TYPE_CHECKING:
    from video_agent.adapters.media.thin_media import LocalArtifactStore

type Rotation = Literal[0, 90, 180, 270]


def _stamp(frame: av.VideoFrame) -> tuple[int, Fraction]:
    if frame.pts is None or frame.time_base is None or frame.time_base <= 0:
        raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "decoded frame lacks a presentation time")
    return frame.pts, Fraction(frame.time_base.numerator, frame.time_base.denominator)


def _decode(
    path: Path, request_ms: int
) -> Generator[tuple[av.VideoFrame, int, Fraction, Fraction]]:
    with ExitStack() as opened:
        options = {"protocol_whitelist": "file"}
        container = opened.enter_context(av.open(str(path), options=options))
        if not container.streams.video:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "registered source contains no video stream")
        stream = container.streams.video[0]
        decoded = container.decode(stream)
        first = next(decoded, None)
        if first is None:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "video stream contains no decodable frame")
        origin_pts, origin_tb = _stamp(first)
        origin_seconds = origin_pts * origin_tb
        frames: Iterator[av.VideoFrame] = chain((first,), decoded)
        if request_ms:
            stream_tb = stream.time_base
            if stream_tb is None or stream_tb <= 0:
                raise MediaError(
                    ErrorCode.TIMESTAMP_UNRELIABLE, "video stream lacks seek time base"
                )
            target = origin_seconds + Fraction(request_ms, 1000)
            try:
                container.seek(
                    math.floor(target / stream_tb), stream=stream, backward=True, any_frame=False
                )
                sought = container.decode(stream)
                landed = next(sought, None)
            except av.FFmpegError:
                landed = None
            if landed is not None:
                pts, tb = _stamp(landed)
            if landed is not None and pts * tb <= target:
                frames = chain((landed,), sought)
            else:
                # Some containers lack a reliable seek index. Read from the known origin.
                reopened = opened.enter_context(av.open(str(path), options=options))
                frames = reopened.decode(video=0)

        previous: Fraction | None = None
        for frame in frames:
            pts, tb = _stamp(frame)
            seconds = pts * tb
            if seconds < origin_seconds or (previous is not None and seconds <= previous):
                raise MediaError(
                    ErrorCode.TIMESTAMP_UNRELIABLE, "unordered presentation timestamps"
                )
            previous = seconds
            yield frame, origin_pts, origin_tb, (seconds - origin_seconds) * 1000


def _rotation(frame: av.VideoFrame) -> Rotation:
    # The angle alone loses the reflection component of a display matrix.
    rotation = frame.rotation % 360
    if Type.DISPLAYMATRIX in frame.side_data:
        data = bytes(frame.side_data[Type.DISPLAYMATRIX])
        if len(data) != 36:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "invalid display matrix size")
        a, b, u, c, d, v, _, _, w = struct.unpack("=9i", data)
        # FFmpeg stores the linear component in signed 16.16 fixed point.
        # Keep only pure quarter turns. The enclosing display box absorbs translation.
        if (a, b, c, d) not in {
            (65536, 0, 0, 65536),
            (0, -65536, 65536, 0),
            (-65536, 0, 0, -65536),
            (0, 65536, -65536, 0),
        } or (u, v, w) != (0, 0, 1 << 30):
            raise MediaError(ErrorCode.CAPABILITY_MISMATCH, "unsupported display matrix")
    if rotation not in (0, 90, 180, 270):
        raise MediaError(ErrorCode.CAPABILITY_MISMATCH, "non-orthogonal video rotation")
    return cast(Rotation, rotation)


def _jpeg(frame: av.VideoFrame, rotation: Rotation, max_edge: int | None) -> bytes:
    image: Image.Image = frame.to_image()  # type: ignore[no-untyped-call]
    if image.mode != "RGB":
        image = image.convert("RGB")
    if rotation:
        transforms = {
            90: Image.Transpose.ROTATE_90,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_270,
        }
        image = image.transpose(transforms[rotation])
    if max_edge is not None and max(image.size) > max_edge:
        scale = max_edge / max(image.size)
        size = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        image = image.resize(size, Image.Resampling.LANCZOS)
    with io.BytesIO() as buf:
        image.save(buf, format="JPEG", quality=92)
        return buf.getvalue()


def extract_frames(
    store: LocalArtifactStore,
    asset: VideoAsset,
    requested_times_ms: list[int],
    *,
    range_ms: TimeRangeMs,
    max_long_edge_px: int | None = None,
) -> list[FrameEvidence]:
    if asset.status != "ready" or not asset.source_artifact_id:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "frame extraction requires a registered source")
    try:
        range_ms.validate_within(asset.duration_ms)
    except ValueError as exc:
        raise MediaError(ErrorCode.INVALID_TIME_RANGE, "window exceeds video duration") from exc
    for request in requested_times_ms:
        if type(request) is not int or request < 0:
            raise MediaError(
                ErrorCode.INVALID_TIMESTAMP, "requested time must be nonnegative integer ms"
            )
        if not range_ms.start_ms <= request < range_ms.end_ms:
            raise MediaError(ErrorCode.NO_FRAME_IN_RANGE, "requested time is outside the window")
    if max_long_edge_px is not None and (
        type(max_long_edge_px) is not int or max_long_edge_px <= 0
    ):
        raise ValueError("max_long_edge_px must be a positive integer")
    if not requested_times_ms:
        return []

    ordered = sorted(enumerate(requested_times_ms), key=lambda pair: pair[1])
    results: dict[int, FrameEvidence] = {}
    cursor = 0
    path = store.resolve_path(asset.source_artifact_id)
    decoded = _decode(path, ordered[0][1])
    try:
        for frame, origin_pts, origin_tb, exact_ms in decoded:
            if exact_ms >= range_ms.end_ms:
                break
            if exact_ms < ordered[cursor][1]:
                continue
            raw_pts, tb = _stamp(frame)
            rotation = _rotation(frame)
            artifact_id = store.put_bytes(
                asset.asset_id, "frame", _jpeg(frame, rotation, max_long_edge_px), suffix=".jpg"
            )
            actual_ms = math.floor(exact_ms)
            while cursor < len(ordered) and ordered[cursor][1] <= exact_ms:
                index, request = ordered[cursor]
                results[index] = FrameEvidence(
                    evidence_id=f"ev_frame_{index + 1:04d}_{actual_ms}ms",
                    asset_id=asset.asset_id,
                    artifact_id=artifact_id,
                    requested_ms=request,
                    actual_pts_ms=actual_ms,
                    raw_pts=raw_pts,
                    duration_ms=asset.duration_ms,
                    time_base=RationalTime(numerator=tb.numerator, denominator=tb.denominator),
                    origin_pts=origin_pts,
                    rotation_deg=rotation,
                    origin_time_base=RationalTime(
                        numerator=origin_tb.numerator, denominator=origin_tb.denominator
                    ),
                )
                cursor += 1
            if cursor == len(ordered):
                break
    except PermissionError as exc:
        raise MediaError(
            ErrorCode.STORAGE_ERROR,
            "permission denied reading source",
            details={"reason": "permission_denied"},
        ) from exc
    except (av.FFmpegError, OSError) as exc:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "registered video could not be decoded") from exc
    finally:
        decoded.close()
    if len(results) != len(ordered):
        raise MediaError(ErrorCode.NO_FRAME_IN_RANGE, "no later presentation frame in the window")
    return [results[index] for index in range(len(ordered))]
