"""Step A integration tests for experiments/media_probe.py.

Every fixture test first runs the independent self-check from
tests/fixtures/media_factory.py before invoking experiments/media_probe.py.
Uses real tmp_path files and real ffmpeg/ffprobe subprocesses with zero mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from experiments.media_probe import (
    MediaProbeError,
    normalize_pts_to_origin_ms,
    probe_media_timeline,
)
from tests.fixtures.media_factory import (
    AUDIO_OFFSET_TARGET_MS,
    AUDIO_STREAM_PTS_TOLERANCE_MS,
    AUDIO_TONE_ONSET_TOLERANCE_MS,
    NONZERO_PTS_TARGET_MS,
    generate_f01_cfr,
    generate_f02_vfr,
    generate_f03_nonzero_pts,
    generate_f04_audio_offset_200ms,
    self_check_f01_cfr,
    self_check_f02_vfr,
    self_check_f03_nonzero_pts,
    self_check_f04_audio_offset_200ms,
)


def test_normalize_pts_to_origin_ms_matches_plan_numeric_example() -> None:
    origin_ms, canonical = normalize_pts_to_origin_ms([5000, 5040, 5120, 5160])
    assert origin_ms == 5000
    assert canonical == [0, 40, 120, 160]

    with pytest.raises(MediaProbeError):
        normalize_pts_to_origin_ms([])

    with pytest.raises(MediaProbeError):
        normalize_pts_to_origin_ms([100, 100, 200])


def test_media_probe_f01_cfr_after_self_check(tmp_path: Path) -> None:
    video_path = tmp_path / "f01_cfr.mp4"
    generate_f01_cfr(video_path)
    self_check = self_check_f01_cfr(video_path, raise_on_fail=True)
    assert self_check.passed is True

    summary = probe_media_timeline(video_path, measure_audio_onset=True)
    assert summary.is_vfr is False
    assert summary.first_video_pts_ms == 0
    assert summary.unique_frame_deltas_ms == [40]
    assert summary.canonical_pts_ms[0] == 0
    assert summary.has_audio is True
    assert summary.audio_tone_onset_ms is not None
    assert summary.audio_tone_onset_ms <= 20


def test_media_probe_f02_vfr_after_self_check(tmp_path: Path) -> None:
    video_path = tmp_path / "f02_vfr.mp4"
    generate_f02_vfr(video_path)
    self_check = self_check_f02_vfr(video_path, raise_on_fail=True)
    assert self_check.passed is True

    summary = probe_media_timeline(video_path)
    assert summary.is_vfr is True
    assert len(summary.unique_frame_deltas_ms) >= 2
    assert all(
        summary.canonical_pts_ms[i + 1] > summary.canonical_pts_ms[i]
        for i in range(len(summary.canonical_pts_ms) - 1)
    )


def test_media_probe_f03_nonzero_origin_after_self_check(tmp_path: Path) -> None:
    video_path = tmp_path / "f03_nonzero_pts.mp4"
    generate_f03_nonzero_pts(video_path)
    self_check = self_check_f03_nonzero_pts(video_path, raise_on_fail=True)
    assert self_check.passed is True

    summary = probe_media_timeline(video_path)
    assert summary.first_video_pts_ms == NONZERO_PTS_TARGET_MS
    assert summary.canonical_pts_ms[0] == 0
    assert summary.canonical_pts_ms[1] == 40
    assert summary.last_video_pts_ms > summary.first_video_pts_ms


def test_media_probe_f04_audio_offset_200ms_after_self_check(tmp_path: Path) -> None:
    video_path = tmp_path / "f04_audio_offset_200ms.mp4"
    generate_f04_audio_offset_200ms(video_path)
    self_check = self_check_f04_audio_offset_200ms(video_path, raise_on_fail=True)
    assert self_check.passed is True

    summary = probe_media_timeline(video_path, measure_audio_onset=True)
    assert summary.has_audio is True
    assert summary.audio_stream_offset_ms is not None
    assert (
        abs(summary.audio_stream_offset_ms - AUDIO_OFFSET_TARGET_MS)
        <= AUDIO_STREAM_PTS_TOLERANCE_MS
    )
    assert summary.audio_tone_onset_ms is not None
    assert (
        abs(summary.audio_tone_onset_ms - AUDIO_OFFSET_TARGET_MS) <= AUDIO_TONE_ONSET_TOLERANCE_MS
    )


def test_media_probe_rejects_corrupted_file(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt_input.mp4"
    corrupt_path.write_bytes(b"not-a-valid-mp4-stream-0000")

    with pytest.raises(MediaProbeError):
        probe_media_timeline(corrupt_path)
