"""Real-media integration tests for M1.5.1 thin PyAV + FFmpeg + ASR chain.

Uses real tmp_path files, real FFmpeg/ffprobe, and real PyAV without mocking
av, subprocess, or the filesystem. Runs independent Fixture Self Check before
asserting production behavior.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from tests.fixtures.media_factory import (
    AUDIO_OFFSET_TARGET_MS,
    AUDIO_STREAM_PTS_TOLERANCE_MS,
    generate_f01_cfr,
    generate_f02_vfr,
    generate_f03_nonzero_pts,
    generate_f04_audio_offset_200ms,
    self_check_f01_cfr,
    self_check_f02_vfr,
    self_check_f03_nonzero_pts,
    self_check_f04_audio_offset_200ms,
)
from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.asr.faster_whisper_thin import FasterWhisperThinTranscriber
from video_agent.adapters.media.thin_media import LocalArtifactStore, PyAVMediaBackend
from video_agent.application.slice import build_sample_timestamps_ms, extract_slice_media
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, RationalTime, TimeRangeMs
from video_agent.domain.ports import (
    ArtifactStore,
    MediaBackend,
    Transcriber,
    TranscribeRequest,
)


def _run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Step pure async coroutine without creating Windows loopback sockets."""
    try:
        coro.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    else:
        coro.close()
        raise AssertionError("coroutine suspended unexpectedly under offline driver")


def _generate_no_audio_mp4(output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=2",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert proc.returncode == 0, f"failed to build no-audio mp4: {proc.stderr}"


def test_thin_adapters_satisfy_domain_ports(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    backend = PyAVMediaBackend(store)
    asr = FasterWhisperThinTranscriber(store, model_path=None)

    assert isinstance(store, ArtifactStore)
    assert isinstance(backend, MediaBackend)
    assert isinstance(asr, Transcriber)


def test_build_sample_timestamps_strict_integers_within_duration() -> None:
    times = build_sample_timestamps_ms(12_000, interval_ms=5000, max_frames=8)
    assert times == [0, 5000, 10000]
    assert all(isinstance(t, int) and not isinstance(t, bool) for t in times)
    assert all(0 <= t < 12_000 for t in times)

    capped = build_sample_timestamps_ms(60_000, interval_ms=1000, max_frames=6)
    assert len(capped) <= 6
    assert capped[0] == 0
    assert capped[-1] < 60_000


def test_pyav_preserves_nonzero_origin_and_real_pts_on_f03(tmp_path: Path) -> None:
    f03_path = tmp_path / "f03_nonzero.mp4"
    cmd = generate_f03_nonzero_pts(f03_path)
    check = self_check_f03_nonzero_pts(f03_path, command=cmd)
    assert check.passed, f"F03 Fixture Self Check failed: {check.failure_reasons}"

    store = LocalArtifactStore(tmp_path / "artifacts")
    backend = PyAVMediaBackend(store)
    asset = backend.probe_video(str(f03_path), asset_id="asset_f03")

    assert asset.status == "ready"
    assert asset.source_artifact_id is not None
    assert asset.has_audio is True
    assert asset.width == 320
    assert asset.height == 240
    assert asset.duration_ms > 0

    # Request 10ms (between 0ms and 40ms frames) and 1010ms (between 1000ms and 1040ms).
    # Actual PTS must snap forward to the next real decoded frame (>= requested_ms),
    # never fabricating actual_pts_ms == requested_ms or ignoring non-zero origin.
    frames = backend.extract_frames(
        asset,
        [0, 10, 1010],
        range_ms=TimeRangeMs(start_ms=0, end_ms=asset.duration_ms),
    )
    assert len(frames) == 3
    assert frames[0].requested_ms == 0
    assert frames[0].actual_pts_ms == 0
    assert frames[0].origin_pts > 0
    assert frames[0].raw_pts == frames[0].origin_pts

    assert frames[1].requested_ms == 10
    assert frames[1].actual_pts_ms == 40
    assert frames[1].actual_pts_ms != frames[1].requested_ms

    assert frames[2].requested_ms == 1010
    assert frames[2].actual_pts_ms == 1040

    # Each extracted frame must resolve to real decodable image bytes in ArtifactStore.
    for frame in frames:
        img_bytes = store.read_bytes(frame.artifact_id)
        with Image.open(io.BytesIO(img_bytes)) as img:
            assert img.size == (320, 240)


def test_pyav_preserves_vfr_pts_and_enforces_half_open_range_on_f02(tmp_path: Path) -> None:
    f02_path = tmp_path / "f02_vfr.mp4"
    cmd = generate_f02_vfr(f02_path)
    check = self_check_f02_vfr(f02_path, command=cmd)
    assert check.passed, f"F02 Fixture Self Check failed: {check.failure_reasons}"

    store = LocalArtifactStore(tmp_path / "artifacts")
    backend = PyAVMediaBackend(store)
    asset = backend.probe_video(str(f02_path), asset_id="asset_f02")

    # Early half of F02 has ~100ms frame intervals; later half has ~40ms intervals.
    frames = backend.extract_frames(
        asset,
        [0, 50, 1600, 1620],
        range_ms=TimeRangeMs(start_ms=0, end_ms=asset.duration_ms),
    )
    assert len(frames) == 4
    assert frames[0].actual_pts_ms == 0
    assert frames[1].actual_pts_ms >= 50
    assert frames[1].actual_pts_ms - frames[0].actual_pts_ms >= 80
    assert frames[3].actual_pts_ms >= 1620
    assert 30 <= (frames[3].actual_pts_ms - frames[2].actual_pts_ms) <= 60

    # Half-open range [0, 80): frame at 0ms exists, next VFR frame is at >= 100ms.
    # Requesting 50ms inside [0, 80) MUST fail with NO_FRAME_IN_RANGE (never borrow >= 80ms).
    with pytest.raises(MediaError) as exc_info:
        backend.extract_frames(asset, [50], range_ms=TimeRangeMs(start_ms=0, end_ms=80))
    assert exc_info.value.code is ErrorCode.NO_FRAME_IN_RANGE


def test_ffmpeg_audio_extract_preserves_200ms_offset_on_f04(tmp_path: Path) -> None:
    f04_path = tmp_path / "f04_offset.mp4"
    cmd = generate_f04_audio_offset_200ms(f04_path)
    check = self_check_f04_audio_offset_200ms(f04_path, command=cmd)
    assert check.passed, f"F04 Fixture Self Check failed: {check.failure_reasons}"

    store = LocalArtifactStore(tmp_path / "artifacts")
    backend = PyAVMediaBackend(store)
    transcriber = FakeTranscriber(scenario="normal")

    result = _run_coro(
        extract_slice_media(
            str(f04_path),
            asset_id="asset_f04",
            media_backend=backend,
            transcriber=transcriber,
            sample_interval_ms=1000,
            max_frames=4,
        )
    )

    assert result.audio_status == "ok"
    assert result.audio_artifact_id is not None
    assert result.audio_timeline is not None
    assert result.audio_timeline.vad_filter is False
    assert result.audio_timeline.leading_silence_padded is False

    # Audio stream offset must be preserved (~200ms within manifest tolerance), never zeroed.
    assert (
        abs(result.audio_timeline.audio_offset_ms - AUDIO_OFFSET_TARGET_MS)
        <= AUDIO_STREAM_PTS_TOLERANCE_MS
    )

    # Extracted audio bytes must be a real RIFF/WAVE file.
    wav_bytes = store.read_bytes(result.audio_artifact_id)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"

    # Transcriber received vad_filter=False and produced canonical shifted segment.
    assert len(transcriber.calls) == 1
    assert transcriber.calls[0].vad_filter is False
    assert len(result.transcript_segments) == 1
    assert result.transcript_segments[0].time_range.start_ms >= (
        AUDIO_OFFSET_TARGET_MS - AUDIO_STREAM_PTS_TOLERANCE_MS
    )


def test_no_audio_stream_distinct_from_corrupt_media(tmp_path: Path) -> None:
    # 1. Valid MP4 with NO audio stream
    no_audio_path = tmp_path / "no_audio.mp4"
    _generate_no_audio_mp4(no_audio_path)

    store = LocalArtifactStore(tmp_path / "artifacts")
    backend = PyAVMediaBackend(store)
    asset = backend.probe_video(str(no_audio_path), asset_id="asset_no_audio")
    assert asset.has_audio is False
    assert asset.audio_codec is None

    with pytest.raises(MediaError) as no_audio_exc:
        backend.extract_audio(asset)
    assert no_audio_exc.value.code is ErrorCode.NO_AUDIO_STREAM

    transcriber = FakeTranscriber(scenario="normal")
    slice_res = _run_coro(
        extract_slice_media(
            str(no_audio_path),
            asset_id="asset_no_audio_slice",
            media_backend=backend,
            transcriber=transcriber,
            sample_interval_ms=1000,
            max_frames=4,
        )
    )
    assert slice_res.audio_status == "no_audio"
    assert slice_res.audio_artifact_id is None
    assert slice_res.transcript_segments == []
    assert len(slice_res.frames) >= 2
    assert len(transcriber.calls) == 0

    # 2. Corrupt / truncated file MUST fail with CORRUPT_MEDIA (distinct from NO_AUDIO_STREAM)
    corrupt_path = tmp_path / "corrupt.mp4"
    corrupt_path.write_bytes(b"\x00\x00\x00\x18ftypmp42truncated_corrupt_payload")

    with pytest.raises(MediaError) as corrupt_exc:
        backend.probe_video(str(corrupt_path), asset_id="asset_corrupt")
    assert corrupt_exc.value.code is ErrorCode.CORRUPT_MEDIA

    # 3. Corrupt audio error from Transcriber MUST NOT be swallowed as no_audio/no_speech
    f01_path = tmp_path / "f01_cfr.mp4"
    cmd = generate_f01_cfr(f01_path)
    check = self_check_f01_cfr(f01_path, command=cmd)
    assert check.passed, f"F01 Fixture Self Check failed: {check.failure_reasons}"

    corrupt_asr = FakeTranscriber(scenario="corrupt_audio")
    with pytest.raises(MediaError) as asr_exc:
        _run_coro(
            extract_slice_media(
                str(f01_path),
                asset_id="asset_corrupt_asr",
                media_backend=backend,
                transcriber=corrupt_asr,
                sample_interval_ms=1000,
                max_frames=3,
            )
        )
    assert asr_exc.value.code is ErrorCode.CORRUPT_MEDIA


def test_faster_whisper_thin_refuses_implicit_download_when_model_missing(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    asr = FasterWhisperThinTranscriber(
        store,
        model_path=tmp_path / "nonexistent_whisper_model",
    )
    req = TranscribeRequest(
        asset_id="asset_001",
        audio_artifact_id="art_audio_001",
        audio_timeline=AudioTimelineMap(
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            audio_offset_ms=0,
            extracted_start_ms=0,
            leading_silence_padded=False,
            vad_filter=False,
        ),
        vad_filter=False,
    )
    with pytest.raises(MediaError) as exc_info:
        _run_coro(asr.transcribe(req))
    assert exc_info.value.code is ErrorCode.DEPENDENCY_MISSING
