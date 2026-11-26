"""Invalidate a loaded local model when files change in its existing directory."""

import multiprocessing
from pathlib import Path

import pytest

from eval.slice_runner import _run_offline_coro
from tests.contract.test_asr import prepare_double, request_for
from video_agent.adapters.asr.faster_whisper import FasterWhisperTranscriber
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.domain.errors import ErrorCode, MediaError


def test_unload_reaps_old_model_and_lazily_reloads_at_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = prepare_double(tmp_path, monkeypatch)
    req = request_for(store)
    asr = FasterWhisperTranscriber(store, model_path=tmp_path)
    try:
        with WorkerLock(tmp_path):
            first = _run_offline_coro(asr.transcribe(req))
            old_pid = asr.worker_pid
            assert old_pid and first.segments[0].text.startswith(f"spawn {old_pid} 1 ")
            (tmp_path / "model.bin").write_bytes(b"revised local weights")
            asr.unload_model()
            assert asr.worker_pid is None and old_pid not in {
                p.pid for p in multiprocessing.active_children()
            }
            asr.unload_model()
            second = _run_offline_coro(asr.transcribe(req))
            new_pid = asr.worker_pid
            assert new_pid and new_pid != old_pid
            assert second.segments[0].text.startswith(f"spawn {new_pid} 1 ")
    finally:
        asr.close()
    with pytest.raises(MediaError) as caught:
        _run_offline_coro(asr.transcribe(req))
    assert caught.value.code == ErrorCode.JOB_CANCELLED and asr.worker_pid is None
