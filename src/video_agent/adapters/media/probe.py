"""Inspect only a managed, self-contained local container."""

import json
import math
import subprocess
from fractions import Fraction

import av

from video_agent.application.ingest import StagedSource
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import VideoAsset

LOCAL_FORMATS = "mov,matroska,webm,avi,mpegts,mpeg,asf,flv,ogg"


def probe_managed(staged: StagedSource, asset_id: str, filename: str) -> VideoAsset:
    entries = (
        "stream=codec_type,codec_name,width,height,duration_ts,time_base:"
        "stream_disposition=attached_pic:format=format_name"
    )
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                LOCAL_FORMATS,
                "-show_entries",
                entries,
                "-of",
                "json",
                str(staged.path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise MediaError(ErrorCode.DEPENDENCY_MISSING, "ffprobe executable is missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(ErrorCode.DEADLINE_EXCEEDED, "ffprobe deadline exceeded") from exc
    except PermissionError as exc:
        raise MediaError(
            ErrorCode.STORAGE_ERROR,
            "ffprobe permission denied",
            details={"reason": "permission_denied"},
        ) from exc
    if result.returncode:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "ffprobe rejected the local container")
    try:
        streams = json.loads(result.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        if video.get("disposition", {}).get("attached_pic"):
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "primary stream is a cover image")
        width, height = int(video["width"]), int(video["height"])
        if max(width, height) > 1920 or min(width, height) > 1080:
            raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "video exceeds P0 dimensions")
        audio = next((s for s in streams if s["codec_type"] == "audio"), None)
        with av.open(
            str(staged.path),
            options={"protocol_whitelist": "file", "format_whitelist": LOCAL_FORMATS},
        ) as container:
            stream = container.streams.video[0]
            first = next(container.decode(stream))
            if first.pts is None or first.time_base is None:
                raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "primary video lacks origin PTS")
            if stream.duration is not None and stream.time_base is not None:
                duration = stream.duration * stream.time_base
            else:
                # Containers without a stream duration may have an unrelated audio tail.
                # Derive the display end from real video frame PTS and packet duration.
                origin = first.pts * first.time_base
                last = first
                for frame in container.decode(stream):
                    last = frame
                if last.pts is None or last.time_base is None or not last.duration:
                    raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "video end is unverified")
                duration = (last.pts + last.duration) * last.time_base - origin
        return VideoAsset(
            asset_id=asset_id,
            source_artifact_id=staged.artifact_id,
            sha256=staged.sha256,
            size_bytes=staged.size_bytes,
            duration_ms=math.ceil(Fraction(duration) * 1000),
            width=width,
            height=height,
            video_codec=video["codec_name"],
            has_audio=audio is not None,
            audio_codec=audio["codec_name"] if audio else None,
            display_filename=filename,
            status="ready",
        )
    except (StopIteration, KeyError, ValueError, ArithmeticError, av.FFmpegError) as exc:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "invalid video stream or metadata") from exc
