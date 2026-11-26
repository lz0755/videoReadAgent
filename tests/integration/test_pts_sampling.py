"""M2.2 real decoder, exact presentation boundaries and playback verification."""

from __future__ import annotations

import io
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import FrameType
from typing import Literal, cast

import av
import pytest
from PIL import Image

from tests.fixtures.p0_media import build_p0_suite
from video_agent.adapters.media.pyav_decoder import extract_frames
from video_agent.adapters.media.thin_media import LocalArtifactStore, PyAVMediaBackend
from video_agent.application.media_timeline import PlaybackVerification, build_playback_map
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import PlaybackVerificationRecord, TimeRangeMs, VideoAsset


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("presentation")
    manifest = build_p0_suite(directory, directory / "manifest.json")
    assert len(manifest["fixtures"]) == 8
    assert all(entry["passed"] for entry in manifest["fixtures"].values())
    return {fid: directory / entry["file_path"] for fid, entry in manifest["fixtures"].items()}


def _registered(path: Path, tmp_path: Path) -> tuple[LocalArtifactStore, VideoAsset]:
    store = LocalArtifactStore(tmp_path / "artifacts")
    asset = PyAVMediaBackend(store).probe_video(str(path), asset_id="asset_pts")
    return store, asset


def _frames_with_pts(path: Path, expr: str) -> list[Fraction]:
    args = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=2:duration=2",
        "-vf",
        f"settb=1/10000,setpts='{expr}'",
        "-enc_time_base",
        "1/10000",
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-g",
        "2",
    ]
    if path.suffix == ".mp4":
        args.extend(["-video_track_timescale", "10000"])
    subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=30)
    times = []
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            assert frame.pts is not None and frame.time_base is not None
            times.append(frame.pts * frame.time_base * 1000)
    return times


def test_long_gop_seeks_to_keyframe_and_keeps_original_request_order(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    store, asset = _registered(media["F07"], tmp_path)
    seeks = 0

    # Observe calls on the real extension object without replacing its implementation.
    def observe(frame: FrameType, event: str, arg: object) -> None:
        nonlocal seeks
        if (
            event == "c_call"
            and getattr(arg, "__name__", None) == "seek"
            and isinstance(getattr(arg, "__self__", None), av.container.InputContainer)
        ):
            seeks += 1

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        frames = extract_frames(
            store, asset, [3200, 3020, 3021], range_ms=TimeRangeMs(start_ms=3000, end_ms=3240)
        )
    finally:
        sys.setprofile(previous)
    assert seeks >= 1
    assert [f.requested_ms for f in frames] == [3200, 3020, 3021]
    assert [f.actual_pts_ms for f in frames] == [3200, 3040, 3040]
    assert len({f.evidence_id for f in frames}) == 3
    assert frames[1].artifact_id == frames[2].artifact_id
    assert all(f.raw_pts * f.time_base.to_fraction() * 1000 == f.actual_pts_ms for f in frames)


def test_real_vfr_and_nonzero_origin(tmp_path: Path, media: dict[str, Path]) -> None:
    store, asset = _registered(media["F02"], tmp_path)
    frames = extract_frames(
        store, asset, [51, 999, 1611], range_ms=TimeRangeMs(start_ms=0, end_ms=2000)
    )
    assert [f.actual_pts_ms for f in frames] == [100, 1000, 1640]
    with pytest.raises(MediaError) as missing:
        extract_frames(store, asset, [901], range_ms=TimeRangeMs(start_ms=0, end_ms=1000))
    assert missing.value.code is ErrorCode.NO_FRAME_IN_RANGE
    store, asset = _registered(media["F03"], tmp_path)
    frames = extract_frames(store, asset, [1010], range_ms=TimeRangeMs(start_ms=1000, end_ms=1100))
    (selected,) = frames
    assert selected.origin_pts * selected.origin_time_base.to_fraction() == 5
    assert selected.raw_pts * selected.time_base.to_fraction() * 1000 == 6040
    assert (selected.requested_ms, selected.actual_pts_ms) == (1010, 1040)


def test_raw_precision_before_floor_and_both_upper_bounds(tmp_path: Path) -> None:
    path = tmp_path / "fine.mp4"
    pts = _frames_with_pts(path, "if(eq(N,0),0,if(eq(N,1),9999,if(eq(N,2),10000,10100)))")
    assert pts == [0, Fraction(9999, 10), 1000, 1010]
    store, asset = _registered(path, tmp_path)
    (frame,) = extract_frames(store, asset, [999], range_ms=TimeRangeMs(start_ms=999, end_ms=1000))
    assert (frame.raw_pts, frame.time_base.to_fraction(), frame.actual_pts_ms) == (
        9999,
        Fraction(1, 10000),
        999,
    )
    (frame,) = extract_frames(
        store, asset, [1000], range_ms=TimeRangeMs(start_ms=1000, end_ms=1001)
    )
    assert (frame.raw_pts, frame.actual_pts_ms) == (10000, 1000)
    with pytest.raises(MediaError) as no_frame:
        extract_frames(store, asset, [1001], range_ms=TimeRangeMs(start_ms=1001, end_ms=1010))
    assert no_frame.value.code is ErrorCode.NO_FRAME_IN_RANGE
    bounded = asset.model_copy(update={"duration_ms": 1010})
    with pytest.raises(MediaError) as at_duration:
        extract_frames(store, bounded, [1001], range_ms=TimeRangeMs(start_ms=0, end_ms=1010))
    assert at_duration.value.code is ErrorCode.NO_FRAME_IN_RANGE


def test_rotation_is_applied_to_actual_jpeg_and_recorded(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    store, asset = _registered(media["F05"], tmp_path)
    (frame,) = extract_frames(
        store, asset, [0], range_ms=TimeRangeMs(start_ms=0, end_ms=1), max_long_edge_px=80
    )
    assert frame.rotation_deg == 90
    with Image.open(io.BytesIO(store.read_bytes(frame.artifact_id))) as img:
        assert img.size == (48, 80)
        r, g, b = cast(tuple[int, int, int], img.getpixel((8, 71)))
        assert r > 180 and max(g, b) < 70
        assert max(cast(tuple[int, int, int], img.getpixel((8, 8)))) < 50


def test_repeated_decoded_pts_are_unreliable(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.mkv"
    assert _frames_with_pts(path, "if(eq(N,2),5000,N*5000)") == [0, 500, 500, 1500]
    store, asset = _registered(path, tmp_path)
    with pytest.raises(MediaError) as repeated:
        extract_frames(store, asset, [0, 1000], range_ms=TimeRangeMs(start_ms=0, end_ms=1600))
    assert repeated.value.code is ErrorCode.TIMESTAMP_UNRELIABLE


def test_missing_pts_is_unreliable(tmp_path: Path, media: dict[str, Path]) -> None:
    store, asset = _registered(media["F01"], tmp_path)
    raw = tmp_path / "no_pts.h264"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media["F01"]),
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "h264",
            str(raw),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with av.open(str(raw)) as container:
        assert next(container.decode(video=0)).pts is None
    asset_id, digest, size = store.put_file_copy("asset_raw", "source_video", raw)
    raw_asset = asset.model_copy(
        update={
            "asset_id": "asset_raw",
            "source_artifact_id": asset_id,
            "sha256": digest,
            "size_bytes": size,
            "duration_ms": 3000,
        }
    )
    with pytest.raises(MediaError) as absent:
        extract_frames(store, raw_asset, [0], range_ms=TimeRangeMs(start_ms=0, end_ms=1))
    assert absent.value.code is ErrorCode.TIMESTAMP_UNRELIABLE


def test_sparse_pts_gap_preserves_3040ms(tmp_path: Path) -> None:
    path = tmp_path / "sparse.mp4"
    assert _frames_with_pts(path, "if(eq(N,0),0,if(eq(N,1),29600,if(eq(N,2),30400,31200)))") == [
        0,
        2960,
        3040,
        3120,
    ]
    store, asset = _registered(path, tmp_path)
    (selected,) = extract_frames(
        store, asset, [3000], range_ms=TimeRangeMs(start_ms=3000, end_ms=3100)
    )
    assert (selected.requested_ms, selected.actual_pts_ms) == (3000, 3040)


def test_playback_verification_is_scoped_to_asset_content_artifact_and_player(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    _, asset = _registered(media["F03"], tmp_path)
    assert asset.source_artifact_id is not None
    with av.open(str(media["F03"])) as container:
        player_pts = []
        for frame in container.decode(video=0):
            assert frame.pts is not None and frame.time_base is not None
            player_pts.append(int(frame.pts * frame.time_base * 1000))
    positions: list[Literal["start", "mid", "end"]] = ["start", "mid", "end"]
    records = tuple(
        PlaybackVerificationRecord(
            position=position,
            source_pts_ms=t - player_pts[0],
            proxy_pts_ms=t,
            observed_offset_ms=player_pts[0],
            tolerance_ms=50,
        )
        for position, t in zip(
            positions,
            [player_pts[0], player_pts[len(player_pts) // 2], player_pts[-1]],
            strict=True,
        )
    )
    verification = PlaybackVerification(
        asset.asset_id, asset.source_artifact_id, asset.sha256, "pyav-test", records
    )
    verified = build_playback_map(asset, player_id="pyav-test", verification=verification)
    assert verified.status == "validated_offset" and verified.offset_ms == 5000
    assert verified.verification_records == list(records)
    drift = records[-1].model_copy(
        update={"observed_offset_ms": 5500, "proxy_pts_ms": player_pts[-1] + 500}
    )
    clustered = tuple(
        record.model_copy(update={"source_pts_ms": i * 40, "proxy_pts_ms": 5000 + i * 40})
        for i, record in enumerate(records)
    )
    for candidate in (
        None,
        replace(verification, asset_id="other"),
        replace(verification, artifact_id="other"),
        replace(verification, source_sha256="0" * 64),
        replace(verification, player_id="browser-x"),
        replace(verification, records=records[:2]),
        replace(verification, records=(*records[:2], drift)),
        replace(verification, records=clustered),
    ):
        fallback = build_playback_map(asset, player_id="pyav-test", verification=candidate)
        assert fallback.status == "preview_only" and fallback.offset_ms is None
