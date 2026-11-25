"""M0.2 media timeline probe for real PTS, VFR detection, origin, and audio offset.

Uses ffprobe (and ffmpeg PCM decode when audio tone onset is requested) with
exact rational-to-floor-millisecond conversion; reports pyav_available honestly.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


class MediaProbeError(RuntimeError):
    """Raised when ffprobe/ffmpeg fails or media lacks reliable timeline metadata."""


@dataclass(frozen=True)
class MediaTimelineSummary:
    """Structured ffprobe timeline observations for a video file."""

    file_name: str
    byte_size: int
    sha256: str
    video_codec: str
    width: int
    height: int
    time_base: str
    video_frame_count: int
    first_video_pts_ms: int
    last_video_pts_ms: int
    duration_ms: int
    canonical_pts_ms: list[int]
    unique_frame_deltas_ms: list[int]
    is_vfr: bool
    has_audio: bool
    audio_codec: str | None
    first_audio_pts_ms: int | None
    audio_stream_offset_ms: int | None
    audio_tone_onset_ms: int | None
    pyav_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "video_codec": self.video_codec,
            "width": self.width,
            "height": self.height,
            "time_base": self.time_base,
            "video_frame_count": self.video_frame_count,
            "first_video_pts_ms": self.first_video_pts_ms,
            "last_video_pts_ms": self.last_video_pts_ms,
            "duration_ms": self.duration_ms,
            "unique_frame_deltas_ms": list(self.unique_frame_deltas_ms),
            "is_vfr": self.is_vfr,
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "first_audio_pts_ms": self.first_audio_pts_ms,
            "audio_stream_offset_ms": self.audio_stream_offset_ms,
            "audio_tone_onset_ms": self.audio_tone_onset_ms,
            "pyav_available": self.pyav_available,
        }


def normalize_pts_to_origin_ms(raw_pts_ms: list[int]) -> tuple[int, list[int]]:
    """Normalize raw integer millisecond PTS values relative to the first valid PTS."""
    if not raw_pts_ms:
        raise MediaProbeError("Cannot normalize empty PTS sequence")
    for idx, value in enumerate(raw_pts_ms):
        if isinstance(value, bool) or not isinstance(value, int):
            raise MediaProbeError(f"PTS at index {idx} must be strict int, got {type(value)}")
        if idx > 0 and value <= raw_pts_ms[idx - 1]:
            raise MediaProbeError(
                f"Non-monotonic PTS at index {idx}: {raw_pts_ms[idx - 1]} -> {value}"
            )
    origin_ms = raw_pts_ms[0]
    canonical = [pts - origin_ms for pts in raw_pts_ms]
    return origin_ms, canonical


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _run_ffprobe_json(args: list[str]) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        raise MediaProbeError("ffprobe binary is not available on PATH")
    proc = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MediaProbeError(f"ffprobe failed (exit={proc.returncode}): {proc.stderr.strip()}")
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"Invalid ffprobe JSON output: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MediaProbeError("ffprobe JSON root is not an object")
    return parsed


def _measure_audio_onset_ms(video_path: Path) -> int | None:
    if shutil.which("ffmpeg") is None:
        raise MediaProbeError("ffmpeg binary is not available on PATH")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-af",
            "aresample=async=1:first_pts=0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0 or len(proc.stdout) < 160:
        return None
    count = len(proc.stdout) // 2
    samples = struct.unpack(f"<{count}h", proc.stdout[: count * 2])
    win = 80  # 5ms at 16 kHz
    step = 16  # 1ms at 16 kHz
    for idx in range(0, len(samples) - win, step):
        seg = samples[idx : idx + win]
        rms = math.sqrt(sum(s * s for s in seg) / win)
        if rms > 1000.0:
            return idx // step
    return None


def probe_media_timeline(
    video_path: Path,
    *,
    measure_audio_onset: bool = False,
) -> MediaTimelineSummary:
    """Probe real decoded video/audio PTS, VFR deltas, origin, and audio offset."""
    if not video_path.is_file():
        raise MediaProbeError(f"Media file does not exist: {video_path.name}")

    meta = _run_ffprobe_json(["-show_format", "-show_streams", str(video_path)])
    streams = meta.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise MediaProbeError(f"No video stream in {video_path.name}")

    v_stream = video_streams[0]
    time_base_str = str(v_stream["time_base"])
    v_tb = Fraction(time_base_str)

    v_frames_payload = _run_ffprobe_json(
        [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pts,pts_time",
            str(video_path),
        ]
    )
    raw_video_pts_ms: list[int] = []
    for frame in v_frames_payload.get("frames", []):
        if frame.get("pts") is not None:
            raw_video_pts_ms.append(math.floor(Fraction(int(frame["pts"])) * v_tb * 1000))
        elif frame.get("pts_time") is not None:
            raw_video_pts_ms.append(math.floor(Fraction(str(frame["pts_time"])) * 1000))

    if not raw_video_pts_ms:
        raise MediaProbeError(f"No decodable video frames with PTS in {video_path.name}")

    first_video_pts_ms, canonical_pts_ms = normalize_pts_to_origin_ms(raw_video_pts_ms)
    last_video_pts_ms = raw_video_pts_ms[-1]
    deltas = [
        canonical_pts_ms[i + 1] - canonical_pts_ms[i] for i in range(len(canonical_pts_ms) - 1)
    ]
    unique_deltas = sorted(set(deltas))
    is_vfr = len(unique_deltas) >= 2 and (max(unique_deltas) - min(unique_deltas)) >= 2

    fmt_duration = meta.get("format", {}).get("duration")
    if fmt_duration is not None:
        duration_ms = math.floor(Fraction(str(fmt_duration)) * 1000)
    else:
        duration_ms = last_video_pts_ms - first_video_pts_ms + (unique_deltas[-1] if deltas else 0)

    has_audio = len(audio_streams) > 0
    audio_codec: str | None = None
    first_audio_pts_ms: int | None = None
    audio_stream_offset_ms: int | None = None
    audio_tone_onset_ms: int | None = None

    if has_audio:
        a_stream = audio_streams[0]
        audio_codec = str(a_stream.get("codec_name"))
        a_tb = Fraction(str(a_stream["time_base"]))
        a_frames_payload = _run_ffprobe_json(
            [
                "-select_streams",
                "a:0",
                "-show_frames",
                "-show_entries",
                "frame=pts,pts_time",
                str(video_path),
            ]
        )
        for frame in a_frames_payload.get("frames", []):
            if frame.get("pts") is not None:
                first_audio_pts_ms = math.floor(Fraction(int(frame["pts"])) * a_tb * 1000)
                break
            if frame.get("pts_time") is not None:
                first_audio_pts_ms = math.floor(Fraction(str(frame["pts_time"])) * 1000)
                break
        if first_audio_pts_ms is not None:
            audio_stream_offset_ms = first_audio_pts_ms - first_video_pts_ms
        if measure_audio_onset:
            audio_tone_onset_ms = _measure_audio_onset_ms(video_path)

    return MediaTimelineSummary(
        file_name=video_path.name,
        byte_size=video_path.stat().st_size,
        sha256=_sha256(video_path),
        video_codec=str(v_stream.get("codec_name")),
        width=int(v_stream["width"]),
        height=int(v_stream["height"]),
        time_base=time_base_str,
        video_frame_count=len(raw_video_pts_ms),
        first_video_pts_ms=first_video_pts_ms,
        last_video_pts_ms=last_video_pts_ms,
        duration_ms=duration_ms,
        canonical_pts_ms=canonical_pts_ms,
        unique_frame_deltas_ms=unique_deltas,
        is_vfr=is_vfr,
        has_audio=has_audio,
        audio_codec=audio_codec,
        first_audio_pts_ms=first_audio_pts_ms,
        audio_stream_offset_ms=audio_stream_offset_ms,
        audio_tone_onset_ms=audio_tone_onset_ms,
        pyav_available=importlib.util.find_spec("av") is not None,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for M0.2 media timeline probing."""
    parser = argparse.ArgumentParser(description="Probe real media PTS, VFR, and audio offset.")
    parser.add_argument("videos", nargs="+", type=Path, help="Video file paths to probe.")
    parser.add_argument(
        "--measure-audio-onset",
        action="store_true",
        help="Also decode audio to PCM and measure tone onset relative to canonical t=0.",
    )
    args = parser.parse_args(argv)
    summaries = [
        probe_media_timeline(path, measure_audio_onset=args.measure_audio_onset).to_dict()
        for path in args.videos
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
