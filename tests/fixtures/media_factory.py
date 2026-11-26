"""F01-F04 synthetic media fixture factory and independent ffprobe/PCM self-check.

Self-check functions deliberately invoke ffprobe/ffmpeg directly and MUST NOT
import or delegate to experiments.media_probe (or future production media code).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

FIXTURE_IDS: tuple[str, ...] = ("F01", "F02", "F03", "F04")
AUDIO_OFFSET_TARGET_MS: int = 200
AUDIO_STREAM_PTS_TOLERANCE_MS: int = 30
AUDIO_TONE_ONSET_TOLERANCE_MS: int = 20
NONZERO_PTS_TARGET_MS: int = 5000
NONZERO_PTS_MIN_MS: int = 1000
MANIFEST_SCHEMA_VERSION: str = "m0.2-fixture-manifest-v1"


class FixtureSelfCheckError(AssertionError):
    """Raised when a synthetic fixture fails independent property verification."""


@dataclass(frozen=True)
class FixtureSelfCheckResult:
    """Independent ffprobe/PCM self-check observations for a single media fixture."""

    fixture_id: str
    passed: bool
    file_path: str
    sha256: str
    byte_size: int
    ffmpeg_version: str
    command: list[str]
    target_property: str
    observations: dict[str, Any]
    assertions: dict[str, bool]
    failure_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "passed": self.passed,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "ffmpeg_version": self.ffmpeg_version,
            "command": list(self.command),
            "target_property": self.target_property,
            "observations": dict(self.observations),
            "assertions": dict(self.assertions),
            "failure_reasons": list(self.failure_reasons),
        }


def _require_binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"Required media binary not found on PATH: {name}")
    return resolved


def get_ffmpeg_version() -> str:
    """Return the first line of `ffmpeg -version`."""
    _require_binary("ffmpeg")
    proc = subprocess.run(
        ["ffmpeg", "-version"],
        check=True,
        capture_output=True,
        text=True,
    )
    first_line = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not first_line:
        raise RuntimeError("Unable to parse ffmpeg -version output")
    return first_line


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest for a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sanitize_command(command: list[str], output_path: Path) -> list[str]:
    """Replace local filesystem output paths with portable filename placeholders."""
    raw_out = str(output_path)
    return [output_path.name if arg == raw_out else arg for arg in command]


def _run_ffmpeg(command: list[str], output_path: Path) -> list[str]:
    _require_binary("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(command, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit={proc.returncode}) for {output_path.name}: {proc.stderr.strip()}"
        )
    return _sanitize_command(command, output_path)


def generate_f01_cfr(output_path: Path, *, visible_clock: bool = False) -> list[str]:
    """Generate F01: 25fps CFR video with visible timecode and 0ms aligned audio."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "25",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        str(output_path),
    ]
    if visible_clock:
        # A bright square advances one column per elapsed second against a black strip.
        filters = ["drawbox=x=0:y=0:w=96:h=24:color=black:t=fill"]
        filters.extend(
            f"drawbox=x={8 + sec * 32}:y=4:w=16:h=16:color=white:t=fill:"
            f"enable='gte(t,{sec})*lt(t,{sec + 1})'"
            for sec in range(3)
        )
        cmd[-1:-1] = ["-vf", ",".join(filters)]
    return _run_ffmpeg(cmd, output_path)


def generate_f02_vfr(output_path: Path) -> list[str]:
    """Generate F02: genuine VFR video with multiple distinct PTS intervals."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=100:duration=3,"
        "select='if(lt(t,1.5),not(mod(n,10)),not(mod(n,4)))'",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-fps_mode",
        "vfr",
        "-video_track_timescale",
        "1000",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        str(output_path),
    ]
    return _run_ffmpeg(cmd, output_path)


def generate_f03_nonzero_pts(output_path: Path) -> list[str]:
    """Generate F03: video whose first decoded valid PTS is non-zero (~5000ms)."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-output_ts_offset",
        f"{NONZERO_PTS_TARGET_MS / 1000:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "25",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        str(output_path),
    ]
    return _run_ffmpeg(cmd, output_path)


def generate_f04_audio_offset_200ms(output_path: Path) -> list[str]:
    """Generate F04: video with audio delayed by ~200ms relative to video origin."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=3",
        "-itsoffset",
        f"{AUDIO_OFFSET_TARGET_MS / 1000:.3f}",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000:duration=2.8",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "25",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        str(output_path),
    ]
    return _run_ffmpeg(cmd, output_path)


def _ffprobe_json(args: list[str]) -> dict[str, Any]:
    _require_binary("ffprobe")
    proc = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FixtureSelfCheckError(f"ffprobe failed: {proc.stderr.strip()}")
    parsed = json.loads(proc.stdout)
    if not isinstance(parsed, dict):
        raise FixtureSelfCheckError("ffprobe did not return a JSON object")
    return parsed


def _inspect_streams_and_pts(path: Path) -> dict[str, Any]:
    """Independently inspect streams and decoded frame PTS via ffprobe."""
    streams_data = _ffprobe_json(["-show_streams", str(path)])
    streams = streams_data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise FixtureSelfCheckError(f"No video stream found in {path.name}")

    v_stream = video_streams[0]
    v_tb = Fraction(str(v_stream["time_base"]))
    v_frames_data = _ffprobe_json(
        [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pts,pts_time",
            str(path),
        ]
    )
    v_pts_ms: list[int] = []
    for frame in v_frames_data.get("frames", []):
        if "pts" in frame and frame["pts"] is not None:
            pts_ms = math.floor(Fraction(int(frame["pts"])) * v_tb * 1000)
            v_pts_ms.append(pts_ms)
        elif "pts_time" in frame and frame["pts_time"] is not None:
            pts_ms = math.floor(Fraction(str(frame["pts_time"])) * 1000)
            v_pts_ms.append(pts_ms)

    deltas_ms = [v_pts_ms[i + 1] - v_pts_ms[i] for i in range(len(v_pts_ms) - 1)]
    unique_deltas_ms = sorted(set(deltas_ms))
    strictly_monotonic = len(v_pts_ms) >= 2 and all(delta > 0 for delta in deltas_ms)

    has_audio = len(audio_streams) > 0
    first_audio_pts_ms: int | None = None
    audio_codec: str | None = None
    if has_audio:
        a_stream = audio_streams[0]
        audio_codec = str(a_stream.get("codec_name"))
        a_tb = Fraction(str(a_stream["time_base"]))
        a_frames_data = _ffprobe_json(
            [
                "-select_streams",
                "a:0",
                "-show_frames",
                "-show_entries",
                "frame=pts,pts_time",
                str(path),
            ]
        )
        for frame in a_frames_data.get("frames", []):
            if "pts" in frame and frame["pts"] is not None:
                first_audio_pts_ms = math.floor(Fraction(int(frame["pts"])) * a_tb * 1000)
                break
            if "pts_time" in frame and frame["pts_time"] is not None:
                first_audio_pts_ms = math.floor(Fraction(str(frame["pts_time"])) * 1000)
                break

    first_video_pts_ms = v_pts_ms[0] if v_pts_ms else None
    last_video_pts_ms = v_pts_ms[-1] if v_pts_ms else None
    audio_stream_offset_ms = (
        first_audio_pts_ms - first_video_pts_ms
        if (first_audio_pts_ms is not None and first_video_pts_ms is not None)
        else None
    )

    return {
        "video_codec": str(v_stream.get("codec_name")),
        "width": int(v_stream["width"]),
        "height": int(v_stream["height"]),
        "video_time_base": str(v_stream["time_base"]),
        "video_frame_count": len(v_pts_ms),
        "first_video_pts_ms": first_video_pts_ms,
        "last_video_pts_ms": last_video_pts_ms,
        "strictly_monotonic_video_pts": strictly_monotonic,
        "unique_video_deltas_ms": unique_deltas_ms,
        "has_audio": has_audio,
        "audio_codec": audio_codec,
        "first_audio_pts_ms": first_audio_pts_ms,
        "audio_stream_offset_ms": audio_stream_offset_ms,
    }


def _measure_pcm_tone_onset_ms(path: Path) -> int | None:
    """Decode audio onto canonical t=0 timeline and detect first audible tone onset (ms)."""
    _require_binary("ffmpeg")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
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
    sample_count = len(proc.stdout) // 2
    samples = struct.unpack(f"<{sample_count}h", proc.stdout[: sample_count * 2])
    window_samples = 80  # 5ms window at 16 kHz
    step_samples = 16  # 1ms step at 16 kHz
    for offset in range(0, len(samples) - window_samples, step_samples):
        window = samples[offset : offset + window_samples]
        rms = math.sqrt(sum(val * val for val in window) / window_samples)
        if rms > 1000.0:
            return offset // step_samples
    return None


def _finalize_result(
    *,
    fixture_id: str,
    path: Path,
    command: list[str] | None,
    target_property: str,
    observations: dict[str, Any],
    assertions: dict[str, bool],
    failure_reasons: list[str],
    raise_on_fail: bool,
) -> FixtureSelfCheckResult:
    passed = len(failure_reasons) == 0 and all(assertions.values())
    result = FixtureSelfCheckResult(
        fixture_id=fixture_id,
        passed=passed,
        file_path=path.name,
        sha256=compute_sha256(path),
        byte_size=path.stat().st_size,
        ffmpeg_version=get_ffmpeg_version(),
        command=_sanitize_command(command, path) if command else [],
        target_property=target_property,
        observations=observations,
        assertions=assertions,
        failure_reasons=failure_reasons,
    )
    if raise_on_fail and not passed:
        raise FixtureSelfCheckError(
            f"{fixture_id} self-check failed ({path.name}): {'; '.join(failure_reasons)}"
        )
    return result


def self_check_f01_cfr(
    path: Path,
    *,
    command: list[str] | None = None,
    raise_on_fail: bool = True,
) -> FixtureSelfCheckResult:
    """Independently verify F01 CFR monotonicity, constant delta, and aligned audio."""
    obs = _inspect_streams_and_pts(path)
    tone_onset_ms = _measure_pcm_tone_onset_ms(path) if obs["has_audio"] else None
    obs["audio_tone_onset_ms"] = tone_onset_ms

    assertions = {
        "has_minimum_frames": obs["video_frame_count"] >= 50,
        "strictly_monotonic_video_pts": bool(obs["strictly_monotonic_video_pts"]),
        "zero_video_origin": obs["first_video_pts_ms"] == 0,
        "constant_frame_delta_cfr": len(obs["unique_video_deltas_ms"]) == 1
        and obs["unique_video_deltas_ms"][0] == 40,
        "has_audio_stream": bool(obs["has_audio"]),
        "audio_aligned_near_zero": (
            obs["audio_stream_offset_ms"] is not None
            and abs(int(obs["audio_stream_offset_ms"])) <= AUDIO_STREAM_PTS_TOLERANCE_MS
        ),
        "audio_tone_onset_near_zero": tone_onset_ms is not None and tone_onset_ms <= 20,
    }
    reasons: list[str] = []
    if not assertions["has_minimum_frames"]:
        reasons.append(f"Expected >=50 video frames, observed {obs['video_frame_count']}")
    if not assertions["strictly_monotonic_video_pts"]:
        reasons.append("Video PTS is not strictly monotonic")
    if not assertions["zero_video_origin"]:
        reasons.append(f"Expected first_video_pts_ms=0, observed {obs['first_video_pts_ms']}")
    if not assertions["constant_frame_delta_cfr"]:
        reasons.append(
            f"Expected constant 40ms CFR delta, observed {obs['unique_video_deltas_ms']}"
        )
    if not assertions["has_audio_stream"]:
        reasons.append("Missing required baseline audio stream")
    if assertions["has_audio_stream"] and not assertions["audio_aligned_near_zero"]:
        reasons.append(
            f"Expected aligned audio offset near 0ms, observed {obs['audio_stream_offset_ms']}ms"
        )
    if assertions["has_audio_stream"] and not assertions["audio_tone_onset_near_zero"]:
        reasons.append(f"Expected audio tone onset <=20ms, observed {tone_onset_ms}ms")

    return _finalize_result(
        fixture_id="F01",
        path=path,
        command=command,
        target_property="Normal 25fps CFR with visible timecode and 0ms aligned audio",
        observations=obs,
        assertions=assertions,
        failure_reasons=reasons,
        raise_on_fail=raise_on_fail,
    )


def self_check_f02_vfr(
    path: Path,
    *,
    command: list[str] | None = None,
    raise_on_fail: bool = True,
) -> FixtureSelfCheckResult:
    """Independently verify F02 has >=2 distinct positive inter-frame PTS intervals."""
    obs = _inspect_streams_and_pts(path)
    deltas = obs["unique_video_deltas_ms"]
    spread_ms = (max(deltas) - min(deltas)) if len(deltas) >= 2 else 0
    obs["delta_spread_ms"] = spread_ms

    assertions = {
        "has_minimum_frames": obs["video_frame_count"] >= 20,
        "strictly_monotonic_video_pts": bool(obs["strictly_monotonic_video_pts"]),
        "multiple_distinct_pts_deltas": len(deltas) >= 2,
        "significant_vfr_spread": spread_ms >= 20,
    }
    reasons: list[str] = []
    if not assertions["has_minimum_frames"]:
        reasons.append(f"Expected >=20 video frames, observed {obs['video_frame_count']}")
    if not assertions["strictly_monotonic_video_pts"]:
        reasons.append("Video PTS is not strictly monotonic")
    if not assertions["multiple_distinct_pts_deltas"]:
        reasons.append(
            f"File is CFR (not genuine VFR): unique inter-frame deltas={deltas}; "
            "at least 2 distinct positive PTS intervals are required"
        )
    elif not assertions["significant_vfr_spread"]:
        reasons.append(f"VFR interval spread {spread_ms}ms is too small (<20ms); deltas={deltas}")

    return _finalize_result(
        fixture_id="F02",
        path=path,
        command=command,
        target_property="Genuine VFR with >=2 distinct positive presentation time intervals",
        observations=obs,
        assertions=assertions,
        failure_reasons=reasons,
        raise_on_fail=raise_on_fail,
    )


def self_check_f03_nonzero_pts(
    path: Path,
    *,
    command: list[str] | None = None,
    raise_on_fail: bool = True,
) -> FixtureSelfCheckResult:
    """Independently verify F03 first decoded video presentation PTS is non-zero."""
    obs = _inspect_streams_and_pts(path)
    first_pts = obs["first_video_pts_ms"]
    assertions = {
        "has_minimum_frames": obs["video_frame_count"] >= 50,
        "strictly_monotonic_video_pts": bool(obs["strictly_monotonic_video_pts"]),
        "first_decoded_pts_is_nonzero": first_pts is not None and int(first_pts) > 0,
        "first_decoded_pts_meets_minimum_offset": (
            first_pts is not None and int(first_pts) >= NONZERO_PTS_MIN_MS
        ),
    }
    reasons: list[str] = []
    if not assertions["has_minimum_frames"]:
        reasons.append(f"Expected >=50 video frames, observed {obs['video_frame_count']}")
    if not assertions["strictly_monotonic_video_pts"]:
        reasons.append("Video PTS is not strictly monotonic")
    if not assertions["first_decoded_pts_is_nonzero"]:
        reasons.append(f"First decoded video PTS is zero or missing: {first_pts}ms")
    elif not assertions["first_decoded_pts_meets_minimum_offset"]:
        reasons.append(
            f"First decoded video PTS {first_pts}ms is below minimum non-zero offset "
            f"{NONZERO_PTS_MIN_MS}ms"
        )

    return _finalize_result(
        fixture_id="F03",
        path=path,
        command=command,
        target_property=f"Non-zero first decoded presentation PTS (~{NONZERO_PTS_TARGET_MS}ms)",
        observations=obs,
        assertions=assertions,
        failure_reasons=reasons,
        raise_on_fail=raise_on_fail,
    )


def self_check_f04_audio_offset_200ms(
    path: Path,
    *,
    command: list[str] | None = None,
    raise_on_fail: bool = True,
) -> FixtureSelfCheckResult:
    """Independently verify F04 audio starts ~200ms after video origin (PTS + tone onset)."""
    obs = _inspect_streams_and_pts(path)
    tone_onset_ms = _measure_pcm_tone_onset_ms(path) if obs["has_audio"] else None
    obs["audio_tone_onset_ms"] = tone_onset_ms
    stream_offset = obs["audio_stream_offset_ms"]

    assertions = {
        "has_audio_stream": bool(obs["has_audio"]),
        "strictly_monotonic_video_pts": bool(obs["strictly_monotonic_video_pts"]),
        "audio_stream_pts_offset_within_tolerance": (
            stream_offset is not None
            and abs(int(stream_offset) - AUDIO_OFFSET_TARGET_MS) <= AUDIO_STREAM_PTS_TOLERANCE_MS
        ),
        "audio_tone_onset_within_tolerance": (
            tone_onset_ms is not None
            and abs(tone_onset_ms - AUDIO_OFFSET_TARGET_MS) <= AUDIO_TONE_ONSET_TOLERANCE_MS
        ),
    }
    reasons: list[str] = []
    if not assertions["has_audio_stream"]:
        reasons.append("Missing required audio stream for F04 audio offset check")
    if not assertions["strictly_monotonic_video_pts"]:
        reasons.append("Video PTS is not strictly monotonic")
    if (
        assertions["has_audio_stream"]
        and not assertions["audio_stream_pts_offset_within_tolerance"]
    ):
        reasons.append(
            f"Audio stream PTS offset {stream_offset}ms is outside "
            f"{AUDIO_OFFSET_TARGET_MS}±{AUDIO_STREAM_PTS_TOLERANCE_MS}ms"
        )
    if assertions["has_audio_stream"] and not assertions["audio_tone_onset_within_tolerance"]:
        reasons.append(
            f"Decoded audio tone onset {tone_onset_ms}ms is outside "
            f"{AUDIO_OFFSET_TARGET_MS}±{AUDIO_TONE_ONSET_TOLERANCE_MS}ms"
        )

    return _finalize_result(
        fixture_id="F04",
        path=path,
        command=command,
        target_property=(
            f"Audio track delayed by {AUDIO_OFFSET_TARGET_MS}ms relative to video origin "
            f"(verified by both stream PTS and decoded waveform tone onset)"
        ),
        observations=obs,
        assertions=assertions,
        failure_reasons=reasons,
        raise_on_fail=raise_on_fail,
    )


def build_f01_f04_suite(
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
    visible_clock: bool = False,
) -> dict[str, Any]:
    """Generate F01-F04 fixtures, self-check every fixture, and optionally write manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        (
            "F01",
            output_dir / "f01_cfr.mp4",
            generate_f01_cfr,
            self_check_f01_cfr,
        ),
        ("F02", output_dir / "f02_vfr.mp4", generate_f02_vfr, self_check_f02_vfr),
        (
            "F03",
            output_dir / "f03_nonzero_pts.mp4",
            generate_f03_nonzero_pts,
            self_check_f03_nonzero_pts,
        ),
        (
            "F04",
            output_dir / "f04_audio_offset_200ms.mp4",
            generate_f04_audio_offset_200ms,
            self_check_f04_audio_offset_200ms,
        ),
    ]

    fixtures_manifest: dict[str, Any] = {}
    for fid, file_path, generator, checker in specs:
        if fid == "F01" and visible_clock:
            cmd = generate_f01_cfr(file_path, visible_clock=True)
        else:
            cmd = generator(file_path)
        result = checker(file_path, command=cmd, raise_on_fail=True)
        fixtures_manifest[fid] = result.to_dict()

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "ffmpeg_version": get_ffmpeg_version(),
        "tolerances": {
            "audio_offset_target_ms": AUDIO_OFFSET_TARGET_MS,
            "audio_stream_pts_tolerance_ms": AUDIO_STREAM_PTS_TOLERANCE_MS,
            "audio_tone_onset_tolerance_ms": AUDIO_TONE_ONSET_TOLERANCE_MS,
            "nonzero_pts_target_ms": NONZERO_PTS_TARGET_MS,
            "nonzero_pts_min_ms": NONZERO_PTS_MIN_MS,
        },
        "fixtures": fixtures_manifest,
    }

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return manifest


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and independently check media fixtures.")
    parser.add_argument("--suite", choices=["m0", "p0"], default="m0")
    parser.add_argument("--check-fixture", choices=["F05", "F06", "F07", "F08"])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Gitignored directory where F01-F04 MP4 files are written.",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=None,
        help="Path to write the sanitized F01-F04 self-check manifest JSON.",
    )
    args = parser.parse_args(argv)
    name = "f01_f08" if args.suite == "p0" or args.check_fixture else "f01_f04"
    output_dir = args.output_dir or Path("data/fixtures") / name
    manifest_out = args.manifest_out or Path("tests/fixtures/manifests") / f"{name}_manifest.json"
    if args.suite == "p0" or args.check_fixture:
        from tests.fixtures.p0_media import build_p0_suite, check_fixture

        if args.check_fixture:
            check_fixture(output_dir, args.check_fixture)
            return 0
        manifest = build_p0_suite(output_dir, manifest_out)
    else:
        manifest = build_f01_f04_suite(output_dir, manifest_path=manifest_out)
    passed_ids = [fid for fid, entry in manifest["fixtures"].items() if entry["passed"]]
    print(
        f"Generated & self-checked {len(passed_ids)}/{len(manifest['fixtures'])} fixtures: "
        f"{', '.join(passed_ids)} -> {manifest_out.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
