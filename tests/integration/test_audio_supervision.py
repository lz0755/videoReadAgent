"""Cancelling FFmpeg also reaps the writer behind executable shims."""

import time
from pathlib import Path

import pytest

from video_agent.adapters.media.ffmpeg_audio import FFmpegAudioExtractor
from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.errors import ErrorCode, MediaError


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_running_ffmpeg_stops_writing_before_supervisor_returns(
    tmp_path: Path, action: str
) -> None:
    output = tmp_path / "slow.pcm"
    extractor = FFmpegAudioExtractor(
        LocalArtifactStore(tmp_path / "store"),
        cancelled=lambda: action == "cancel" and output.is_file() and output.stat().st_size > 0,
    )
    cmd = [
        "-nostdin",
        "-v",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "sine=d=5:r=16000",
        "-flush_packets",
        "1",
        "-f",
        "s16le",
        str(output),
    ]
    with pytest.raises(MediaError) as caught:
        extractor._run(cmd, time.monotonic() + (1.5 if action == "timeout" else 8))
    assert caught.value.code == (
        ErrorCode.JOB_CANCELLED if action == "cancel" else ErrorCode.DEADLINE_EXCEEDED
    )
    size = output.stat().st_size
    assert 0 < size < 16000 * 5 * 2
    time.sleep(0.35)
    assert output.stat().st_size == size
    output.unlink()
