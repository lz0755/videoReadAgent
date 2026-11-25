"""Step A integration tests for F01-F04 synthetic media fixtures and independent self-check.

Uses real tmp_path files and real ffmpeg/ffprobe subprocesses; zero mocking of
subprocess, av, sqlite3, or the filesystem.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.media_factory import (
    AUDIO_OFFSET_TARGET_MS,
    AUDIO_STREAM_PTS_TOLERANCE_MS,
    AUDIO_TONE_ONSET_TOLERANCE_MS,
    FIXTURE_IDS,
    NONZERO_PTS_MIN_MS,
    FixtureSelfCheckError,
    build_f01_f04_suite,
    generate_f01_cfr,
    self_check_f01_cfr,
    self_check_f02_vfr,
    self_check_f03_nonzero_pts,
    self_check_f04_audio_offset_200ms,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)")


def test_build_f01_f04_suite_passes_all_independent_self_checks(tmp_path: Path) -> None:
    manifest_path = tmp_path / "f01_f04_manifest.json"
    manifest = build_f01_f04_suite(tmp_path / "media", manifest_path=manifest_path)

    assert manifest_path.is_file()
    raw_text = manifest_path.read_text(encoding="utf-8")
    assert not PRIVATE_PATH_PATTERN.search(raw_text)

    fixtures = manifest["fixtures"]
    assert set(fixtures.keys()) == set(FIXTURE_IDS)
    for fid in FIXTURE_IDS:
        entry = fixtures[fid]
        assert entry["passed"] is True, f"{fid} failed self-check: {entry.get('failure_reasons')}"
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["byte_size"] > 0
        assert entry["ffmpeg_version"].startswith("ffmpeg version")
        assert isinstance(entry["command"], list) and len(entry["command"]) >= 4

    f01_obs = fixtures["F01"]["observations"]
    assert f01_obs["first_video_pts_ms"] == 0
    assert len(f01_obs["unique_video_deltas_ms"]) == 1
    assert f01_obs["has_audio"] is True

    f02_obs = fixtures["F02"]["observations"]
    assert len(f02_obs["unique_video_deltas_ms"]) >= 2

    f03_obs = fixtures["F03"]["observations"]
    assert f03_obs["first_video_pts_ms"] >= NONZERO_PTS_MIN_MS

    f04_obs = fixtures["F04"]["observations"]
    assert (
        abs(f04_obs["audio_stream_offset_ms"] - AUDIO_OFFSET_TARGET_MS)
        <= AUDIO_STREAM_PTS_TOLERANCE_MS
    )
    assert (
        abs(f04_obs["audio_tone_onset_ms"] - AUDIO_OFFSET_TARGET_MS)
        <= AUDIO_TONE_ONSET_TOLERANCE_MS
    )


def test_f02_self_check_fails_when_given_ordinary_cfr_file(tmp_path: Path) -> None:
    cfr_path = tmp_path / "ordinary_cfr.mp4"
    generate_f01_cfr(cfr_path)

    with pytest.raises(FixtureSelfCheckError, match="(?i)vfr|distinct|cfr"):
        self_check_f02_vfr(cfr_path, raise_on_fail=True)

    report = self_check_f02_vfr(cfr_path, raise_on_fail=False)
    assert report.passed is False
    assert len(report.failure_reasons) >= 1


def test_f03_self_check_fails_when_given_zero_origin_file(tmp_path: Path) -> None:
    zero_origin_path = tmp_path / "nonzero_in_name_only.mp4"
    generate_f01_cfr(zero_origin_path)

    with pytest.raises(FixtureSelfCheckError, match="(?i)non-zero|nonzero|pts"):
        self_check_f03_nonzero_pts(zero_origin_path, raise_on_fail=True)


def test_f04_self_check_fails_when_given_zero_offset_audio_file(tmp_path: Path) -> None:
    aligned_audio_path = tmp_path / "aligned_audio_0ms.mp4"
    generate_f01_cfr(aligned_audio_path)

    with pytest.raises(FixtureSelfCheckError, match="(?i)offset|200|audio"):
        self_check_f04_audio_offset_200ms(aligned_audio_path, raise_on_fail=True)


def test_f01_self_check_fails_when_audio_stream_missing(tmp_path: Path) -> None:
    silent_path = tmp_path / "no_audio_cfr.mp4"
    subprocess.run(
        [
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
            str(silent_path),
        ],
        check=True,
    )

    with pytest.raises(FixtureSelfCheckError, match="(?i)audio"):
        self_check_f01_cfr(silent_path, raise_on_fail=True)


def test_repo_f01_f04_manifest_is_valid_and_sanitized() -> None:
    manifest_path = REPO_ROOT / "tests" / "fixtures" / "manifests" / "f01_f04_manifest.json"
    assert manifest_path.is_file(), "Committed F01-F04 self-check manifest must exist"
    raw_text = manifest_path.read_text(encoding="utf-8")
    assert not PRIVATE_PATH_PATTERN.search(raw_text)

    data = json.loads(raw_text)
    assert data["schema_version"] == "m0.2-fixture-manifest-v1"
    assert set(data["fixtures"].keys()) == set(FIXTURE_IDS)
    for fid in FIXTURE_IDS:
        entry = data["fixtures"][fid]
        assert entry["passed"] is True
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["ffmpeg_version"].startswith("ffmpeg version")
