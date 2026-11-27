"""OS-held single-worker / migration process lock helper (M1.4).

Uses portalocker exclusive non-blocking OS locks on data_root/worker.lock.
Never unlinks or replaces the lock file; marks the file descriptor non-inheritable
so compute children (FFmpeg/ASR) never inherit the worker lock.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from io import TextIOWrapper
from pathlib import Path
from types import TracebackType
from typing import Literal

import portalocker

from video_agent.domain.errors import ErrorCode, StorageError, VideoAgentError, WorkerLockError


class WorkerLock:
    """Exclusive non-blocking OS file lock on data_root/worker.lock."""

    def __init__(
        self, data_root: Path, *, filename: Literal["worker.lock", "import.lock"] = "worker.lock"
    ) -> None:
        self.data_root = data_root
        self.lock_path = data_root / filename
        self._handle: TextIOWrapper | None = None

    def acquire(self) -> None:
        """Acquire exclusive non-blocking OS lock or raise WORKER_ALREADY_RUNNING."""
        if self._handle is not None:
            return
        self.data_root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            os.set_inheritable(handle.fileno(), False)
            portalocker.lock(
                handle,
                portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
            )
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            self._handle = handle
        except portalocker.exceptions.LockException as exc:
            handle.close()
            raise WorkerLockError(
                ErrorCode.WORKER_ALREADY_RUNNING,
                "worker or migration lock is already held for this data_root",
            ) from exc
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        """Release OS lock cleanly without unlinking or replacing the lock file."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            portalocker.unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> WorkerLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


def ensure_compute_idle(root: Path) -> None:
    if (root / "compute-active").exists():
        raise StorageError(ErrorCode.RESOURCE_BUSY, "previous compute children require inspection")


@contextmanager
def compute_guard(root: Path, identifier: str, *, may_spawn: bool) -> Iterator[None]:
    """The caller owns worker.lock. A hard exit leaves a durable uncertainty marker."""
    ensure_compute_idle(root)
    if not may_spawn:
        yield
        return
    marker = root / "compute-active"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()} {identifier}\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield
    except VideoAgentError as exc:
        if exc.code != ErrorCode.RESOURCE_BUSY:
            marker.unlink()
        raise
    except Exception:
        # The supervised use case has unwound and closed its compute children.
        marker.unlink()
        raise
    else:
        marker.unlink()
