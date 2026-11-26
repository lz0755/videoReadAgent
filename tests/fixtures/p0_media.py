"""Independent synthetic P0 media measurements, with portable generation recipes."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import av
from PIL import Image

from tests.fixtures.media_factory import (
    FixtureSelfCheckError,
    _ffprobe_json,
    build_f01_f04_suite,
    compute_sha256,
)

_NAMES = {
    "F05": "f05_rotation.mp4",
    "F06": "f06_no_audio.mp4",
    "F07": "f07_long_gop.mp4",
    "F08": "f08_truncated.mp4",
}
_TARGETS = {
    "F05": "90 degree display matrix moves the red top-left marker to bottom-left",
    "F06": "decodable video without any audio stream",
    "F07": "two-second GOP with reordered B frames and backward keyframe seek",
    "F08": "known prefix of F07 truncated before the MP4 moov atom",
}


def _generate(directory: Path) -> dict[str, list[list[str]]]:
    def ffmpeg(args: list[str]) -> list[str]:
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *args]
        subprocess.run(cmd, cwd=directory, check=True, capture_output=True, timeout=30)
        return cmd

    video = ["-f", "lavfi", "-i"]
    encoder = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    source = "testsrc2=size=160x96:rate=25:duration=5"
    commands = {
        "F06": [ffmpeg([*video, source, "-an", *encoder, _NAMES["F06"]])],
        "F07": [
            ffmpeg(
                [
                    *video,
                    source,
                    "-an",
                    *encoder,
                    "-g",
                    "50",
                    "-bf",
                    "3",
                    "-x264-params",
                    "min-keyint=50:scenecut=0:b-adapt=0",
                    _NAMES["F07"],
                ]
            )
        ],
    }
    raw = "f05_encoded.mp4"
    marker = "color=black:size=160x96:rate=25:d=1,drawbox=x=8:y=8:w=24:h=24:color=red:t=fill"
    commands["F05"] = [
        ffmpeg([*video, marker, *encoder, raw]),
        ffmpeg(["-display_rotation:v:0", "90", "-i", raw, "-c", "copy", _NAMES["F05"]]),
    ]
    source_path = directory / _NAMES["F07"]
    cut = source_path.stat().st_size // 2
    with source_path.open("rb") as reader, (directory / _NAMES["F08"]).open("wb") as writer:
        writer.write(reader.read(cut))
    commands["F08"] = [["copy_prefix", _NAMES["F07"], str(cut), _NAMES["F08"]]]
    return commands


def _require(fid: str, condition: bool, detail: str) -> None:
    if not condition:
        raise FixtureSelfCheckError(f"{fid}: {detail}")


def _pixels(frame: av.VideoFrame) -> Image.Image:
    image: Image.Image = frame.to_image()  # type: ignore[no-untyped-call]
    return image


def _first_four(path: Path, fid: str, obs: dict[str, Any]) -> None:
    times = []
    marker_columns = []
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            assert frame.pts is not None and frame.time_base is not None
            t = frame.pts * frame.time_base * 1000
            times.append(t)
            if fid == "F01" and t in (0, 1000, 2000):
                pixels = _pixels(frame).convert("L")
                cells = [cast(int, pixels.getpixel((16 + 32 * i, 12))) for i in range(3)]
                _require(fid, sum(v > 220 for v in cells) == 1, "missing visible time marker")
                marker_columns.append(next(i for i, v in enumerate(cells) if v > 220))
    _require(
        fid,
        len(times) == obs["video_frame_count"]
        and int(times[0]) == obs["first_video_pts_ms"]
        and int(times[-1]) == obs["last_video_pts_ms"]
        and sorted({int(b) - int(a) for a, b in zip(times[:-1], times[1:], strict=True)})
        == obs["unique_video_deltas_ms"],
        "PyAV and ffprobe presentation timelines disagree",
    )
    obs["pyav"] = {"frames": len(times), "first_ms": int(times[0]), "last_ms": int(times[-1])}
    if fid == "F01":
        _require(fid, marker_columns == [0, 1, 2], "time marker disagrees with elapsed seconds")
        obs["visible_marker_seconds"] = marker_columns
    if fid == "F04":
        with av.open(str(path)) as container:
            audio = next(container.decode(audio=0))
            assert audio.pts is not None and audio.time_base is not None
            audio_ms = int(audio.pts * audio.time_base * 1000)
        _require(fid, audio_ms == obs["first_audio_pts_ms"], "decoded audio origins disagree")
        obs["pyav"]["first_audio_ms"] = audio_ms


def _rotation(path: Path) -> dict[str, Any]:
    data = _ffprobe_json(["-show_streams", "-select_streams", "v:0", str(path)])
    side = data["streams"][0].get("side_data_list", [])
    rotation = next(
        (s.get("rotation") for s in side if s.get("side_data_type") == "Display Matrix"), None
    )
    _require("F05", rotation == 90, "expected a 90 degree display matrix")
    with av.open(str(path)) as container:
        raw = _pixels(next(container.decode(video=0))).convert("RGB")
    displayed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    upright = Image.frombytes("RGB", (raw.height, raw.width), displayed.stdout)

    def red_corner(img: Image.Image) -> str:
        rgb = img.convert("RGB")
        points = {
            "top_left": (16, 16),
            "top_right": (img.width - 17, 16),
            "bottom_left": (16, img.height - 17),
            "bottom_right": (img.width - 17, img.height - 17),
        }
        red = []
        for name, (x, y) in points.items():
            r, g, b = cast(tuple[int, int, int], rgb.getpixel((x, y)))
            if r > 180 and max(g, b) < 60:
                red.append(name)
        return red[0] if len(red) == 1 else "ambiguous"

    raw_corner, display_corner = red_corner(raw), red_corner(upright)
    _require(
        "F05", (raw_corner, display_corner) == ("top_left", "bottom_left"), "marker orientation"
    )
    return {
        "rotation_degrees": rotation,
        "pyav": {"raw_marker_corner": raw_corner},
        "display_marker_corner": display_corner,
    }


def _gop(path: Path) -> dict[str, Any]:
    probe = _ffprobe_json(
        [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pict_type,key_frame,pts_time",
            str(path),
        ]
    )
    frames = probe["frames"]
    keys = [int(Fraction(f["pts_time"]) * 1000) for f in frames if f["key_frame"]]
    b_count = sum(f["pict_type"] == "B" for f in frames)
    _require("F07", keys[:2] == [0, 2000] and b_count > 0, "long GOP or B frames missing")
    target_ms = 3020
    selected: list[tuple[int, bytes]] = []
    landed = None
    for seek in (False, True):
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            assert stream.time_base is not None
            if seek:
                container.seek(int(Fraction(target_ms, 1000) / stream.time_base), stream=stream)
            for frame in container.decode(stream):
                assert frame.pts is not None and frame.time_base is not None
                ms = int(frame.pts * frame.time_base * 1000)
                if seek and landed is None:
                    _require("F07", frame.key_frame, "seek did not land on a keyframe")
                    landed = ms
                if ms >= target_ms:
                    selected.append((ms, _pixels(frame).tobytes()))
                    break
    _require(
        "F07",
        len(selected) == 2 and landed == 2000 and selected[0] == selected[1],
        "sequential and keyframe-seek output disagree",
    )
    return {
        "pyav": av.__version__,
        "keyframe_times_ms": keys,
        "b_frames": b_count,
        "seek_landed_ms": landed,
        "requested_ms": target_ms,
        "sequential_selected_ms": selected[0][0],
        "seek_selected_ms": selected[1][0],
    }


def check_fixture(directory: Path, fid: str) -> dict[str, Any]:
    path = directory / _NAMES[fid]
    if fid == "F05":
        return _rotation(path)
    if fid == "F06":
        meta = _ffprobe_json(["-show_streams", str(path)])
        count = sum(s["codec_type"] == "audio" for s in meta["streams"])
        with av.open(str(path)) as container:
            frame = next(container.decode(video=0), None)
            valid = frame is not None and not container.streams.audio
        _require(fid, count == 0 and valid, "expected decodable video and zero audio streams")
        return {"audio_stream_count": count, "pyav": av.__version__}
    if fid == "F07":
        return _gop(path)
    original = directory / _NAMES["F07"]
    cut = path.stat().st_size
    size = original.stat().st_size
    with original.open("rb") as handle:
        _require(
            fid,
            0 < cut == size // 2 and path.read_bytes() == handle.read(cut),
            "expected the known truncated prefix of F07",
        )
    result = subprocess.run(["ffprobe", "-v", "error", str(path)], capture_output=True, timeout=30)
    missing_moov = result.returncode != 0 and b"moov atom not found" in result.stderr
    _require(fid, missing_moov, "expected missing moov atom")
    try:
        with av.open(str(path)) as container:
            next(container.decode(video=0), None)
    except av.error.InvalidDataError:
        return {
            "missing_moov": True,
            "pyav": "InvalidDataError",
            "truncation_offset": cut,
            "original_bytes": size,
            "original_sha256": compute_sha256(original),
        }
    raise FixtureSelfCheckError("F08: truncated input unexpectedly decoded")


def build_p0_suite(directory: Path, manifest_out: Path) -> dict[str, Any]:
    manifest = build_f01_f04_suite(directory, visible_clock=True)
    manifest["schema_version"] = "m2.0-fixture-manifest-v1"
    manifest["pyav_version"] = av.__version__
    entries = manifest["fixtures"]
    for fid, entry in entries.items():
        _first_four(directory / entry["file_path"], fid, entry["observations"])
        entry["assertions"]["pyav_agrees_with_ffprobe"] = True
    commands = _generate(directory)
    for fid, name in _NAMES.items():
        path = directory / name
        obs = check_fixture(directory, fid)
        entries[fid] = {
            "file_path": name,
            "fixture_id": fid,
            "passed": True,
            "target_property": _TARGETS[fid],
            "sha256": compute_sha256(path),
            "byte_size": path.stat().st_size,
            "ffmpeg_version": manifest["ffmpeg_version"],
            "command": commands[fid][-1],
            "generation_steps": commands[fid],
            "observations": obs,
            "assertions": {_TARGETS[fid]: True},
            "failure_reasons": [],
        }
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
