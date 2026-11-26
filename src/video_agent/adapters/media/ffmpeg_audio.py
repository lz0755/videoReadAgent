"""Supervised local FFmpeg extraction of canonical video windows."""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import av

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, RationalTime, TimeRangeMs, VideoAsset

if TYPE_CHECKING:
    from video_agent.adapters.media.thin_media import LocalArtifactStore


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # FFmpeg must not start via its package-manager shim until the launcher is
    # inside the job. A pipe gate avoids racing AssignProcessToJobObject.
    _LAUNCH = (
        "import subprocess,sys\n"
        "if sys.stdin.buffer.read(1) != b'G': sys.exit(127)\n"
        "try:\n"
        " p = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, close_fds=True)\n"
        "except PermissionError: sys.exit(126)\n"
        "except FileNotFoundError: sys.exit(127)\n"
        "sys.exit(p.returncode)"
    )

    class _JobLimits(ctypes.Structure):
        # JOBOBJECT_BASIC_LIMIT_INFORMATION followed by the IO and memory fields
        # of JOBOBJECT_EXTENDED_LIMIT_INFORMATION (native alignment).
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_ws", ctypes.c_size_t),
            ("max_ws", ctypes.c_size_t),
            ("process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
            ("io", ctypes.c_uint64 * 6),
            ("memory", ctypes.c_size_t * 4),
        ]

    @contextmanager
    def _windows_job(cmd: list[str]) -> Iterator[subprocess.Popen[bytes]]:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.OpenProcess.restype = wintypes.HANDLE
        handle = wintypes.HANDLE(api.CreateJobObjectW(None, None))
        if not handle:
            raise MediaError(ErrorCode.RESOURCE_BUSY, "unable to create FFmpeg job")
        process: subprocess.Popen[bytes] | None = None
        try:
            limits = _JobLimits(flags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise MediaError(ErrorCode.RESOURCE_BUSY, "unable to supervise FFmpeg job")
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _LAUNCH, *cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            child = wintypes.HANDLE(api.OpenProcess(0x101, False, process.pid))
            try:
                if not child or not api.AssignProcessToJobObject(handle, child):
                    raise MediaError(ErrorCode.RESOURCE_BUSY, "unable to bind FFmpeg job")
            finally:
                if child:
                    api.CloseHandle(child)
            assert process.stdin is not None
            process.stdin.write(b"G")
            process.stdin.close()
            yield process
        finally:
            try:
                # Terminate all descendants, including the binary behind a shim.
                if not api.TerminateJobObject(handle, 1):
                    raise MediaError(ErrorCode.RESOURCE_BUSY, "unable to terminate FFmpeg job")
                accounting = (wintypes.DWORD * 12)()  # 4 LARGE_INTEGERs, 4 DWORDs
                reap_deadline = time.monotonic() + 5
                while True:
                    if not api.QueryInformationJobObject(
                        handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                    ):
                        raise MediaError(ErrorCode.RESOURCE_BUSY, "unable to inspect FFmpeg job")
                    if accounting[10] == 0:  # ActiveProcesses
                        break
                    if time.monotonic() >= reap_deadline:
                        raise MediaError(ErrorCode.RESOURCE_BUSY, "FFmpeg job still running")
                    time.sleep(0.01)
            finally:
                api.CloseHandle(handle)
                if process is not None:
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)


@contextmanager
def _ffmpeg_process(cmd: list[str]) -> Iterator[subprocess.Popen[bytes]]:
    if sys.platform == "win32":
        with _windows_job(cmd) as process:
            yield process
    else:
        with subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        ) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    except ProcessLookupError:
                        process.wait(timeout=5)


def _origins(path: Path) -> tuple[int, Fraction, Fraction]:
    # Demux independently from the beginning for each stream. A video seek to
    # t=0 can discard audio leading the first video frame.
    with av.open(str(path)) as container:
        first = next(container.decode(video=0), None)
        if first is None or first.pts is None or first.time_base is None:
            raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "missing video origin")
        video_pts = first.pts
        video_tb = Fraction(first.time_base.numerator, first.time_base.denominator)
    with av.open(str(path)) as audio_container:
        if not audio_container.streams.audio:
            raise MediaError(ErrorCode.NO_AUDIO_STREAM, "no audio stream")
        audio = next(audio_container.decode(audio=0), None)
        if audio is None:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "audio stream has no decodable samples")
        if audio.pts is None or audio.time_base is None:
            raise MediaError(ErrorCode.TIMESTAMP_UNRELIABLE, "missing first audio PTS")
        audio_tb = Fraction(audio.time_base.numerator, audio.time_base.denominator)
        offset_ms = (audio.pts * audio_tb - video_pts * video_tb) * 1000
    return video_pts, video_tb, offset_ms


class FFmpegAudioExtractor:
    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        executable: str = "ffmpeg",
        timeout_seconds: float = 120.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._store = store
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._cancelled = cancelled

    def _remaining(self, deadline: float) -> float:
        if self._cancelled is not None and self._cancelled():
            raise MediaError(ErrorCode.JOB_CANCELLED, "audio extraction cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaError(ErrorCode.DEADLINE_EXCEEDED, "audio extraction timed out")
        return remaining

    def _run(self, args: list[str], deadline: float) -> None:
        self._remaining(deadline)
        if Path(self._executable).is_dir():
            raise PermissionError("cannot execute a directory")
        executable = shutil.which(self._executable)
        if executable is None:
            if Path(self._executable).exists():
                raise PermissionError("cannot execute FFmpeg binary")
            raise MediaError(ErrorCode.DEPENDENCY_MISSING, "FFmpeg executable missing")
        with _ffmpeg_process([executable, *args]) as process:
            while process.poll() is None:
                timeout = min(self._remaining(deadline), 0.05)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    continue
            self._remaining(deadline)
            if process.returncode == 126:
                raise PermissionError("cannot execute FFmpeg binary")
            if process.returncode:
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "FFmpeg could not decode the audio")

    def extract_audio(
        self, asset: VideoAsset, *, range_ms: TimeRangeMs | None = None
    ) -> tuple[str, AudioTimelineMap]:
        deadline = time.monotonic() + self._timeout_seconds
        self._remaining(deadline)
        if not asset.has_audio:
            raise MediaError(ErrorCode.NO_AUDIO_STREAM, "asset has no audio stream")
        if not asset.source_artifact_id:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "asset has no managed source")
        start, end = (
            (0, asset.duration_ms) if range_ms is None else (range_ms.start_ms, range_ms.end_ms)
        )
        if end > asset.duration_ms:
            raise MediaError(ErrorCode.INVALID_TIME_RANGE, "audio window extends beyond video")
        source = self._store.resolve_path(asset.source_artifact_id)
        try:
            origin, time_base, offset = _origins(source)
            offset_ms = math.floor(offset)
            filters = ["asetpts=PTS-STARTPTS", "aresample=16000:async=1:first_pts=0"]
            padded = range_ms is not None and offset > start
            if padded:
                filters.append(f"adelay={math.ceil((offset - start) * 16)}S:all=1")
                first_ms = Fraction(start)
                extracted_start_ms = start
            else:
                trim = max(0, math.ceil((start - offset) * 16))
                if trim:
                    filters.append(f"atrim=start_sample={trim}")
                first_ms = offset + Fraction(trim, 16)
                extracted_start_ms = math.floor(first_ms) - offset_ms
            sample_limit = math.floor((end - first_ms) * 16)
            if sample_limit <= 0:
                raise MediaError(ErrorCode.INVALID_TIME_RANGE, "no audio coverage in video window")
            filters.append(f"atrim=end_sample={sample_limit}")
            timeline = AudioTimelineMap(
                origin_pts=origin,
                origin_time_base=RationalTime(
                    numerator=time_base.numerator, denominator=time_base.denominator
                ),
                audio_offset_ms=offset_ms,
                extracted_start_ms=extracted_start_ms,
                leading_silence_padded=padded,
            )
            with tempfile.TemporaryDirectory(
                dir=self._store.root_dir, prefix=".audio-"
            ) as directory:
                wav = Path(directory) / "audio.wav"
                self._run(
                    [
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-af",
                        ",".join(filters),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        str(wav),
                    ],
                    deadline,
                )
                with wave.open(str(wav), "rb") as reader:
                    count = reader.getnframes()
                    if not count:
                        raise MediaError(ErrorCode.INVALID_TIME_RANGE, "no audio samples in window")
                    if count > sample_limit:
                        raise MediaError(ErrorCode.CORRUPT_MEDIA, "audio exceeds requested window")
                    reader.setpos(count - 1)
                    if len(reader.readframes(1)) != 2:
                        raise MediaError(ErrorCode.CORRUPT_MEDIA, "incomplete extracted PCM")
                self._remaining(deadline)
                artifact, _, _ = self._store.put_file_copy(asset.asset_id, "audio", wav)
            return artifact, timeline
        except PermissionError as exc:
            raise MediaError(
                ErrorCode.STORAGE_ERROR,
                "audio access denied",
                details={"reason": "permission_denied"},
            ) from exc
        except FileNotFoundError as exc:
            raise MediaError(ErrorCode.STORAGE_ERROR, "managed audio input missing") from exc
        except (av.FFmpegError, wave.Error, EOFError) as exc:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "invalid audio stream or PCM") from exc
        except OSError as exc:
            raise MediaError(ErrorCode.STORAGE_ERROR, "local audio I/O failed") from exc
