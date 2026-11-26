"""Exercise the exact PCM input contract of the local faster-whisper adapter."""

import io
import struct
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from eval.slice_runner import _run_offline_coro
from video_agent.adapters.asr.faster_whisper_thin import FasterWhisperThinTranscriber
from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, RationalTime
from video_agent.domain.ports import TranscribeRequest


@pytest.mark.parametrize("sample_rate", [16000, 8000])
def test_whisper_receives_normalized_mono_pcm_or_rejects_wrong_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_rate: int
) -> None:
    samples = struct.pack("<hhh", -32768, 0, 32767)
    data = io.BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setparams((1, 2, sample_rate, 3, "NONE", "not compressed"))
        writer.writeframes(samples)
    with wave.open(io.BytesIO(data.getvalue()), "rb") as reader:
        assert (reader.getframerate(), reader.getnframes()) == (sample_rate, 3)
        assert reader.readframes(3) == samples

    store = LocalArtifactStore(tmp_path / "artifacts")
    asr = FasterWhisperThinTranscriber(store)
    model = Mock()
    model.transcribe.return_value = ([], SimpleNamespace(language="en"))
    monkeypatch.setattr(asr, "_get_or_load_model", lambda: model)
    request = TranscribeRequest(
        asset_id="asset_pcm",
        audio_artifact_id=store.put_bytes("asset_pcm", "audio", data.getvalue(), suffix=".wav"),
        audio_timeline=AudioTimelineMap(
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=16000),
            audio_offset_ms=200,
            extracted_start_ms=0,
            leading_silence_padded=False,
        ),
        vad_filter=False,
    )
    if sample_rate != 16000:
        with pytest.raises(MediaError) as caught:
            _run_offline_coro(asr.transcribe(request))
        assert caught.value.code is ErrorCode.CORRUPT_MEDIA
        model.transcribe.assert_not_called()
        return

    result = _run_offline_coro(asr.transcribe(request))
    model.transcribe.assert_called_once()
    waveform = model.transcribe.call_args.args[0]
    assert isinstance(waveform, np.ndarray) and waveform.dtype == np.float32
    np.testing.assert_array_equal(waveform, [-1.0, 0.0, 32767 / 32768])
    assert model.transcribe.call_args.kwargs["vad_filter"] is False
    assert result.segments == []
