"""Run explicitly with preinstalled model weights and a locally synthesized voice."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from eval.slice_runner import _run_offline_coro
from video_agent.adapters.asr.faster_whisper import FasterWhisperTranscriber
from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.models import AudioTimelineMap, RationalTime
from video_agent.domain.ports import TranscribeRequest

pytestmark = pytest.mark.model_local


def test_local_whisper_after_40_seconds_of_silence(tmp_path: Path) -> None:
    model = Path(
        os.environ.get(
            "VIDEO_AGENT_ASR_MODEL_PATH",
            "data/models/faster-whisper-small/536b0662742c02347bc0e980a01041f333bce120",
        )
    )
    assert (model / "model.bin").is_file(), "this explicit job requires preinstalled weights"
    words = "The blue train arrives at noon."
    raw = tmp_path / "speech.wav"
    if sys.platform == "win32":
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voice.SelectVoiceByHints('NotSet','NotSet',0,[cultureinfo]'en-US'); "
            "$voice.SetOutputToWaveFile((Join-Path (Get-Location) 'speech.wav')); "
            "$voice.Speak('The blue train arrives at noon.'); $voice.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=tmp_path,
            timeout=30,
            check=True,
            capture_output=True,
        )
    else:
        synth = shutil.which("espeak-ng") or shutil.which("espeak")
        assert synth is not None, "this explicit job requires installed offline espeak"
        subprocess.run([synth, "-w", str(raw), "-v", "en", words], check=True, timeout=30)
    path = tmp_path / "delayed.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(raw),
            "-af",
            "aresample=16000,adelay=40000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    with wave.open(str(path)) as reader:
        assert (reader.getnchannels(), reader.getframerate(), reader.getsampwidth()) == (
            1,
            16000,
            2,
        )
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    voiced = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 500)
    assert 40000 <= int(voiced[0]) // 16 <= 40500
    assert 41000 <= int(voiced[-1]) // 16 <= 43500
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact, _, _ = store.put_file_copy("speech", "audio", path)
    request = TranscribeRequest(
        asset_id="speech",
        audio_artifact_id=artifact,
        audio_timeline=AudioTimelineMap(
            origin_pts=0,
            audio_offset_ms=0,
            extracted_start_ms=0,
            origin_time_base=RationalTime(numerator=1, denominator=16000),
        ),
        language="en",
    )
    asr = FasterWhisperTranscriber(store, model_path=model, timeout_seconds=180)
    try:
        response = _run_offline_coro(asr.transcribe(request))
        train = [s for s in response.segments if "train" in s.text.lower()]
        assert train and any(39000 <= s.time_range.start_ms <= 41000 for s in train)
        assert all(s.time_range.end_ms <= len(pcm) // 16 for s in response.segments)
    finally:
        asr.close()
