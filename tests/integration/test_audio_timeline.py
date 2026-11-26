"""Synthetic audio with independently measured samples and video/audio PTS."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import av
import numpy as np
import pytest

from tests.fixtures.media_factory import (
    generate_f04_audio_offset_200ms,
    self_check_f04_audio_offset_200ms,
)
from video_agent.adapters.media.ffmpeg_audio import FFmpegAudioExtractor
from video_agent.adapters.media.thin_media import LocalArtifactStore, PyAVMediaBackend
from video_agent.application.audio_timeline import map_audio_range
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, RationalTime, TimeRangeMs, VideoAsset


def span(start: int, end: int) -> TimeRangeMs:
    return TimeRangeMs(start_ms=start, end_ms=end)


@pytest.fixture(scope="module")
def delayed(tmp_path_factory: pytest.TempPathFactory) -> tuple[LocalArtifactStore, VideoAsset]:
    folder = tmp_path_factory.mktemp("offset")
    path = folder / "late.mp4"
    command = generate_f04_audio_offset_200ms(path)
    assert self_check_f04_audio_offset_200ms(path, command=command).passed
    store = LocalArtifactStore(folder / "artifacts")
    return store, PyAVMediaBackend(store).probe_video(str(path), asset_id="late")


def samples(path: Path) -> np.ndarray[tuple[int], np.dtype[np.int16]]:
    with wave.open(str(path)) as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            2,
            16000,
        )
        count = reader.getnframes()
        raw = reader.readframes(count)
    assert 0 < count == len(raw) // 2
    return np.frombuffer(raw, dtype="<i2")


def test_full_and_window_crossing_audio_start(
    delayed: tuple[LocalArtifactStore, VideoAsset],
) -> None:
    store, asset = delayed
    extractor = FFmpegAudioExtractor(store)
    audio, timeline = extractor.extract_audio(asset)
    pcm = samples(store.resolve_path(audio))
    assert 170 <= timeline.audio_offset_ms <= 230 and not timeline.leading_silence_padded
    assert timeline.extracted_start_ms == 0 and timeline.vad_filter is False
    first_sample = int(np.flatnonzero(np.abs(pcm) > 1000)[0])
    assert 190 <= timeline.audio_offset_ms + first_sample // 16 <= 210
    assert timeline.audio_offset_ms + len(pcm) // 16 <= asset.duration_ms

    window, mapping = extractor.extract_audio(asset, range_ms=span(100, 500))
    section = samples(store.resolve_path(window))
    assert mapping.leading_silence_padded and mapping.audio_offset_ms == timeline.audio_offset_ms
    assert len(section) == 400 * 16
    first_sample = int(np.flatnonzero(np.abs(section) > 1000)[0])
    assert 90 <= first_sample // 16 <= 110
    assert map_audio_range(mapping, span(100, 300)) == span(200, 400)

    before, mapping = extractor.extract_audio(asset, range_ms=span(5, 85))
    silent = samples(store.resolve_path(before))
    assert len(silent) == 80 * 16 and np.count_nonzero(silent) == 0
    assert mapping.leading_silence_padded and map_audio_range(mapping, span(0, 80)) == span(5, 85)


def test_chunk_offsets_applied_once_to_real_tone(tmp_path: Path) -> None:
    path = tmp_path / "ninety.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x64:r=1:d=91",
            "-itsoffset",
            "0.2",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='0.5*sin(2*PI*440*t)*between(t,81.8,82.8)':s=16000:d=90",
            "-c:v",
            "libx264",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=45,
    )
    # Independent decode checks the actual waveform location as well as stream origins.
    with av.open(str(path)) as container:
        video = next(container.decode(video=0))
        assert video.time == 0
    with av.open(str(path)) as container:
        tone_frames = []
        for i, frame in enumerate(container.decode(audio=0)):
            assert frame.pts is not None and frame.time_base is not None
            if i == 0:
                assert frame.pts * frame.time_base * 1000 == 200
            if np.abs(frame.to_ndarray()).max() > 1000:
                tone_frames.append(float(frame.pts * frame.time_base))
        assert 81.9 < tone_frames[0] <= 82 and 82.9 < tone_frames[-1] <= 83
    store = LocalArtifactStore(tmp_path / "artifacts")
    asset = PyAVMediaBackend(store).probe_video(str(path), asset_id="chunk")
    artifact, mapping = FFmpegAudioExtractor(store).extract_audio(
        asset, range_ms=span(80000, 90000)
    )
    pcm = samples(store.resolve_path(artifact))
    active = np.flatnonzero(np.abs(pcm) > 1000)
    assert len(pcm) == 160000
    assert 1999 <= int(active[0]) // 16 <= 2001 and 2998 <= int(active[-1]) // 16 <= 3001
    assert mapping.audio_offset_ms == 200
    assert map_audio_range(mapping, span(2000, 3000)) == span(82000, 83000)


def test_mapping_clips_both_ends_without_fabricating_time() -> None:
    timeline = AudioTimelineMap(
        origin_pts=5,
        origin_time_base=RationalTime(numerator=1, denominator=1),
        audio_offset_ms=-200,
        extracted_start_ms=0,
    )
    assert map_audio_range(timeline, span(10, 199), duration_ms=1000) is None
    assert map_audio_range(timeline, span(150, 300), duration_ms=1000) == span(0, 100)
    assert map_audio_range(timeline, span(1150, 1500), duration_ms=1000) == span(950, 1000)
    assert map_audio_range(timeline, span(1200, 3000), duration_ms=1000) is None


@pytest.mark.parametrize("failure", ["missing", "permissions", "timeout", "cancelled", "range"])
def test_extraction_errors_do_not_publish_artifacts(
    delayed: tuple[LocalArtifactStore, VideoAsset], failure: str, tmp_path: Path
) -> None:
    store, asset = delayed
    before = {p for p in store.root_dir.rglob("*") if p.is_file()}
    executable = {"missing": str(tmp_path / "missing.exe"), "permissions": str(tmp_path)}.get(
        failure, "ffmpeg"
    )
    extractor = FFmpegAudioExtractor(
        store,
        executable=executable,
        timeout_seconds=0.000001 if failure == "timeout" else 30,
        cancelled=lambda: failure == "cancelled",
    )
    codes = {
        "missing": ErrorCode.DEPENDENCY_MISSING,
        "permissions": ErrorCode.STORAGE_ERROR,
        "timeout": ErrorCode.DEADLINE_EXCEEDED,
        "cancelled": ErrorCode.JOB_CANCELLED,
        "range": ErrorCode.INVALID_TIME_RANGE,
    }
    time_range = span(0, asset.duration_ms + 1) if failure == "range" else None
    with pytest.raises(MediaError) as caught:
        extractor.extract_audio(asset, range_ms=time_range)
    assert caught.value.code == codes[failure]
    if failure == "permissions":
        assert caught.value.details["reason"] == "permission_denied"
    assert before == {p for p in store.root_dir.rglob("*") if p.is_file()}


def test_damaged_track_is_distinct_from_missing_track(
    delayed: tuple[LocalArtifactStore, VideoAsset], tmp_path: Path
) -> None:
    original_store, original = delayed
    assert original.source_artifact_id
    source = original_store.resolve_path(original.source_artifact_id)
    good_video = tmp_path / "video.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v", "-c", "copy", str(good_video)],
        check=True,
    )
    damaged = tmp_path / "damaged.mp4"
    positions = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_packets",
                "-show_entries",
                "packet=pos,size",
                "-of",
                "json",
                str(source),
            ]
        )
    )["packets"]
    assert positions
    damaged.write_bytes(source.read_bytes())
    with damaged.open("r+b") as handle:
        for packet in positions:
            handle.seek(int(packet["pos"]))
            handle.write(b"\xff" * int(packet["size"]))
    with av.open(str(good_video)) as container:
        assert not container.streams.audio and next(container.decode(video=0)).pts is not None
    with av.open(str(damaged)) as container:
        assert container.streams.audio and next(container.decode(video=0)).pts is not None
    check = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(damaged),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
    )
    assert check.returncode != 0
    store = LocalArtifactStore(tmp_path / "artifacts")
    for source, code in [
        (good_video, ErrorCode.NO_AUDIO_STREAM),
        (damaged, ErrorCode.CORRUPT_MEDIA),
    ]:
        asset = PyAVMediaBackend(store).probe_video(str(source), asset_id=source.stem)
        with pytest.raises(MediaError) as caught:
            FFmpegAudioExtractor(store).extract_audio(asset)
        assert caught.value.code == code
