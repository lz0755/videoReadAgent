"""Offline faster-whisper in a supervised persistent spawn process."""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
import sys
import threading
import time
import wave
import weakref
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from video_agent.application.audio_timeline import map_audio_range
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import TimeRangeMs, TranscriptSegment
from video_agent.domain.ports import TranscribeRequest, TranscribeResponse

if TYPE_CHECKING:
    from video_agent.adapters.media.thin_media import LocalArtifactStore

if sys.platform == "win32":
    from multiprocessing.connection import PipeConnection as WorkerConnection
else:
    from multiprocessing.connection import Connection as WorkerConnection


def _load_model(model_path: Path, device: str, compute_type: str) -> Any:
    # This runs only inside the spawned worker, before any SDK import.
    os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    _check_weights(model_path)
    return WhisperModel(
        str(model_path), device=device, compute_type=compute_type, local_files_only=True
    )


def _check_weights(path: Path | None) -> None:
    if path is None:
        raise MediaError(ErrorCode.DEPENDENCY_MISSING, "local model directory required")
    try:
        # The SDK falls back to a Hub tokenizer when tokenizer.json is missing.
        for name in ("model.bin", "config.json", "tokenizer.json"):
            with (path / name).open("rb") as handle:
                if not handle.read(1):
                    raise MediaError(ErrorCode.DEPENDENCY_MISSING, "empty local model file")
    except PermissionError as exc:
        raise MediaError(
            ErrorCode.STORAGE_ERROR, "model access denied", details={"reason": "permission_denied"}
        ) from exc
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
        raise MediaError(ErrorCode.DEPENDENCY_MISSING, "local model files missing") from exc


@dataclass
class _Output:
    segments: list[tuple[int, int, str]]
    language: str | None


def _infer(model: Any, path: Path, language: str | None) -> _Output:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            handle.seek(0)
            with wave.open(handle) as reader:
                if (reader.getnchannels(), reader.getframerate(), reader.getsampwidth()) != (
                    1,
                    16000,
                    2,
                ):
                    raise MediaError(
                        ErrorCode.CORRUPT_MEDIA, "expected mono 16 kHz signed 16-bit PCM"
                    )
                count = reader.getnframes()
                pcm = reader.readframes(min(count, os.fstat(handle.fileno()).st_size // 2))
        legacy_pipe = header[4:8] == b"\xff" * 4 and count == 0x7FFFFFFF
        if not pcm or len(pcm) % 2 or (not legacy_pipe and len(pcm) != count * 2):
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "empty or truncated PCM")
    except (wave.Error, EOFError) as exc:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "invalid PCM container") from exc
    data = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / np.float32(32768)
    if not np.any(data):
        return _Output([], language)
    duration_ms = len(data) // 16
    # The first word can occur anywhere in Whisper's 30-second acoustic window.
    raw, info = model.transcribe(
        data, language=language, vad_filter=False, word_timestamps=True, max_initial_timestamp=30.0
    )
    records = []
    for segment in raw:
        text = segment.text.strip()
        if not text:
            continue
        try:
            start = math.floor(Fraction(str(segment.start)) * 1000)
            end = math.floor(Fraction(str(segment.end)) * 1000)
        except (ValueError, OverflowError) as exc:
            raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "non-finite ASR timestamp") from exc
        if start < 0 or end < start:
            raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "invalid ASR segment order")
        end = min(end, duration_ms)
        if start < end:
            records.append((start, end, text))
    return _Output(records, getattr(info, "language", None) or language)


def _compute(
    connection: WorkerConnection,
    path: Path,
    device: str,
    compute_type: str,
    load: Callable[[Path, str, str], Any],
) -> None:
    model = None
    with connection:
        while True:
            try:
                source, language = connection.recv()
            except EOFError, OSError:
                return
            try:
                if model is None:
                    model = load(path, device, compute_type)
                result = _infer(model, source, language)
                connection.send(result)
            except MediaError as exc:
                connection.send((exc.code, exc.details))
            except PermissionError:
                connection.send((ErrorCode.STORAGE_ERROR, {"reason": "permission_denied"}))
            except FileNotFoundError, ImportError:
                connection.send((ErrorCode.DEPENDENCY_MISSING, {}))
            except MemoryError:
                connection.send((ErrorCode.RESOURCE_BUSY, {}))
            except Exception as exc:
                code = ErrorCode.DEPENDENCY_MISSING if model is None else ErrorCode.RESOURCE_BUSY
                connection.send((code, {"reason": type(exc).__name__}))


def _reap(process: BaseProcess, connection: WorkerConnection) -> None:
    connection.close()
    if process.is_alive():
        process.terminate()
        process.join(1)
    if process.is_alive():
        process.kill()
        process.join(2)
    if process.is_alive():
        raise MediaError(ErrorCode.RESOURCE_BUSY, "previous ASR process is still running")
    process.join()
    process.close()


class FasterWhisperTranscriber:
    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        model_path: Path | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        timeout_seconds: float = 120.0,
        cancelled: Callable[[], bool] | None = None,
        duration_for_asset: Callable[[str], int] | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._store = store
        self._path = model_path.resolve() if model_path is not None else None
        self._device = device
        self._compute_type = compute_type
        self._timeout = timeout_seconds
        self._cancelled = cancelled
        self._duration = duration_for_asset
        self._process: BaseProcess | None = None
        self._connection: WorkerConnection | None = None
        self._finalizer: (
            weakref.finalize[[BaseProcess, WorkerConnection], FasterWhisperTranscriber] | None
        ) = None
        self._lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def worker_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def _stop(self) -> None:
        if self._process is not None and self._connection is not None:
            _reap(self._process, self._connection)
            if self._finalizer is not None:
                self._finalizer.detach()
            self._process = None
            self._connection = None

    def _channel(self) -> WorkerConnection:
        if self._process is not None and self._connection is not None:
            if self._process.is_alive() and not self._connection.closed:
                return self._connection
            self._stop()
        _check_weights(self._path)
        assert self._path is not None
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        proc = context.Process(
            target=_compute,
            daemon=True,
            args=(
                child,
                self._path,
                self._device,
                self._compute_type,
                _load_model,
            ),
        )
        try:
            proc.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._connection, self._process = parent, proc
        self._finalizer = weakref.finalize(self, _reap, proc, parent)
        return parent

    def _remaining(self, deadline: float, cancelled: threading.Event) -> float:
        if self._closed.is_set() or cancelled.is_set() or (self._cancelled and self._cancelled()):
            raise MediaError(ErrorCode.JOB_CANCELLED, "ASR cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaError(ErrorCode.DEADLINE_EXCEEDED, "ASR deadline elapsed")
        return remaining

    def _run(self, request: TranscribeRequest, cancellation: threading.Event) -> TranscribeResponse:
        if request.vad_filter is not False or request.audio_timeline.vad_filter is not False:
            raise MediaError(
                ErrorCode.CAPABILITY_MISMATCH, "P0 transcription requires vad_filter=false"
            )
        if not self._lock.acquire(blocking=False):
            raise MediaError(ErrorCode.RESOURCE_BUSY, "ASR already processing a request")
        try:
            deadline = time.monotonic() + self._timeout
            self._remaining(deadline, cancellation)
            channel = self._channel()
            source = self._store.resolve_path(request.audio_artifact_id)
            duration = self._duration(request.asset_id) if self._duration is not None else None
            channel.send((source, request.language))
            while not channel.poll(min(0.05, self._remaining(deadline, cancellation))):
                if self._process is None or not self._process.is_alive():
                    raise MediaError(ErrorCode.RESOURCE_BUSY, "ASR child exited during inference")
            result = channel.recv()
            self._remaining(deadline, cancellation)
            if not isinstance(result, _Output):
                code, details = result
                raise MediaError(code, "local ASR failed", details=details)
            segments = []
            for idx, (begin, end, text) in enumerate(result.segments, 1):
                interval = map_audio_range(
                    request.audio_timeline,
                    TimeRangeMs(start_ms=begin, end_ms=end),
                    duration_ms=duration,
                )
                if interval is not None:
                    segments.append(
                        TranscriptSegment(
                            evidence_id=f"ev_asr_{idx:04d}_{interval.start_ms}ms",
                            asset_id=request.asset_id,
                            time_range=interval,
                            text=text,
                            asr_version="faster-whisper-v2",
                            audio_timeline=request.audio_timeline,
                            language=result.language,
                        )
                    )
            return TranscribeResponse(
                segments=segments, asr_version="faster-whisper-v2", language=result.language
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._stop()
            raise MediaError(
                ErrorCode.RESOURCE_BUSY, "ASR process or transport unavailable"
            ) from exc
        except BaseException:
            self._stop()
            raise
        finally:
            self._lock.release()

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        cancellation = threading.Event()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # The repository's synchronous local CLI may explicitly drive the coroutine.
            return self._run(request, cancellation)
        pending = loop.run_in_executor(None, self._run, request, cancellation)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancellation.set()
            with suppress(MediaError):
                await asyncio.shield(pending)
            raise

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._stop()

    def unload_model(self) -> None:
        """Release idle model resources so the next call can load new local weights."""
        if not self._lock.acquire(blocking=False):
            raise MediaError(ErrorCode.RESOURCE_BUSY, "cannot unload an active ASR request")
        try:
            self._stop()
        finally:
            self._lock.release()
