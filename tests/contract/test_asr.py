"""Exercise the real spawn/IPC boundary with a deterministic local inference model."""

from __future__ import annotations

import gc
import io
import multiprocessing
import os
import sqlite3
import time
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from eval.slice_runner import _run_offline_coro
from video_agent.adapters.asr import faster_whisper
from video_agent.adapters.asr.faster_whisper import FasterWhisperTranscriber
from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, RationalTime, TimeRangeMs
from video_agent.domain.ports import TranscribeRequest


class ModelDouble:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls = 0

    def transcribe(
        self,
        waveform: np.ndarray[tuple[int], np.dtype[np.float32]],
        *,
        language: str | None,
        vad_filter: bool,
        **kwargs: object,
    ) -> tuple[list[SimpleNamespace], SimpleNamespace]:
        assert not vad_filter and waveform.dtype == np.float32
        self.calls += 1
        if language == "de":
            (self.directory / "busy").touch()
            time.sleep(30)
        if language == "fr":
            return [], SimpleNamespace(language="fr")
        if language == "ja":
            raise PermissionError("example")
        if language == "it":
            raise MemoryError("example")
        # The parent owns a real WorkerLock and an open SQLite connection here.
        # Also check for an explicitly inheritable file referring to the lock inode.
        lock_stat = (self.directory / "worker.lock").stat()
        inherited = False
        for fd in range(3, 256):
            try:
                inherited |= os.path.samestat(os.fstat(fd), lock_stat)
            except OSError:
                continue
        has_db = any(isinstance(obj, sqlite3.Connection) for obj in gc.get_objects())
        detail = (
            f"{multiprocessing.get_start_method()} {os.getpid()} {self.calls} {inherited} {has_db}"
        )
        assert len(waveform) == 16000 * 44
        assert np.count_nonzero(waveform[: 16000 * 40]) == 0
        return [
            SimpleNamespace(start=40.0, end=43.0, text=detail),
            SimpleNamespace(start=43.5, end=52.0, text="clipped"),
            SimpleNamespace(start=55.0, end=58.0, text="outside"),
        ], SimpleNamespace(language=language)


def local_model_double(directory: Path, device: str, compute_type: str) -> ModelDouble:
    return ModelDouble(directory)


def request_for(store: LocalArtifactStore, *, malformed: bool = False) -> TranscribeRequest:
    pcm = np.zeros(16000 * 44, dtype="<i2")
    pcm[16000 * 40 : 16000 * 43] = 4000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setparams((1, 2, 16000, len(pcm), "NONE", "not compressed"))
        writer.writeframes(pcm.tobytes())
    data = buf.getvalue()
    if malformed:
        data = data[:-200]
    return TranscribeRequest(
        asset_id="video",
        audio_artifact_id=store.put_bytes("video", "audio", data, suffix=".wav"),
        audio_timeline=AudioTimelineMap(
            origin_pts=800,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            audio_offset_ms=200,
            extracted_start_ms=0,
        ),
        language="en",
    )


def prepare_double(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalArtifactStore:
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(faster_whisper, "_load_model", local_model_double)
    return LocalArtifactStore(tmp_path / "artifacts")


def test_spawn_reuses_model_and_does_not_inherit_lock_or_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_double(tmp_path, monkeypatch)
    req = request_for(store)
    asr = FasterWhisperTranscriber(store, model_path=tmp_path, duration_for_asset=lambda _: 43900)
    with WorkerLock(tmp_path), closing(sqlite3.connect(tmp_path / "state.sqlite")) as database:
        database.execute("CREATE TABLE sample (id INTEGER)")
        database.execute("BEGIN IMMEDIATE")
        with (tmp_path / "worker.lock").open("rb") as handle:
            os.set_inheritable(handle.fileno(), True)
            try:
                outputs = [_run_offline_coro(asr.transcribe(req)) for _ in range(2)]
                pid = asr.worker_pid
                assert pid is not None and pid != os.getpid()
                for count, result in enumerate(outputs, 1):
                    spoken, tail = result.segments
                    assert spoken.text == f"spawn {pid} {count} False False"
                    assert spoken.time_range == TimeRangeMs(start_ms=40200, end_ms=43200)
                    assert tail.time_range == TimeRangeMs(start_ms=43700, end_ms=43900)
                    assert result.language == "en" and spoken.audio_timeline == req.audio_timeline
            finally:
                asr.close()
    asr.close()
    assert asr.worker_pid is None


@pytest.mark.parametrize("name", ["model.bin", "tokenizer.json"])
def test_missing_weights_or_tokenizer_rejected_before_spawn(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_double(tmp_path, monkeypatch)
    (tmp_path / name).unlink()
    asr = FasterWhisperTranscriber(store, model_path=tmp_path)
    with pytest.raises(MediaError) as caught:
        _run_offline_coro(asr.transcribe(request_for(store)))
    assert caught.value.code == ErrorCode.DEPENDENCY_MISSING and asr.worker_pid is None


@pytest.mark.parametrize("failure", ["no_speech", "corrupt", "denied", "memory", "vad"])
def test_asr_keeps_status_and_artifact_failure_distinct(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_double(tmp_path, monkeypatch)
    req = request_for(store, malformed=failure == "corrupt")
    if failure == "vad":
        with pytest.raises(ValidationError):
            TranscribeRequest.model_validate({**req.model_dump(), "vad_filter": True})
        req = req.model_copy(update={"vad_filter": True})
    else:
        req = req.model_copy(
            update={
                "language": {"no_speech": "fr", "denied": "ja", "memory": "it"}.get(failure, "en")
            }
        )
    asr = FasterWhisperTranscriber(store, model_path=tmp_path)
    try:
        if failure == "no_speech":
            result = _run_offline_coro(asr.transcribe(req))
            assert result.segments == [] and result.language == "fr"
        else:
            with pytest.raises(MediaError) as caught:
                _run_offline_coro(asr.transcribe(req))
            assert (
                caught.value.code
                == {
                    "corrupt": ErrorCode.CORRUPT_MEDIA,
                    "denied": ErrorCode.STORAGE_ERROR,
                    "memory": ErrorCode.RESOURCE_BUSY,
                    "vad": ErrorCode.CAPABILITY_MISMATCH,
                }[failure]
            )
            if failure == "denied":
                assert caught.value.details.get("reason") == "permission_denied"
            if failure == "vad":
                assert asr.worker_pid is None
    finally:
        asr.close()


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_parent_stops_busy_compute_and_reaps_child(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_double(tmp_path, monkeypatch)
    req = request_for(store).model_copy(update={"language": "de"})
    busy = tmp_path / "busy"
    asr = FasterWhisperTranscriber(
        store,
        model_path=tmp_path,
        timeout_seconds=4 if action == "timeout" else 12,
        cancelled=lambda: action == "cancel" and busy.is_file(),
    )
    before = {p.pid for p in multiprocessing.active_children()}
    try:
        with pytest.raises(MediaError) as caught:
            _run_offline_coro(asr.transcribe(req))
        assert busy.is_file(), "the child must have entered inference"
        assert caught.value.code == (
            ErrorCode.JOB_CANCELLED if action == "cancel" else ErrorCode.DEADLINE_EXCEEDED
        )
        assert asr.worker_pid is None
        assert {p.pid for p in multiprocessing.active_children()} <= before
    finally:
        asr.close()
