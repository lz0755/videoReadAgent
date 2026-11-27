"""Single-worker process entrypoint (M5)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.jobs import SQLiteJobs
from video_agent.adapters.storage.worker_lock import WorkerLock, compute_guard, ensure_compute_idle
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import Job


def run_queue(
    root: Path,
    jobs: SQLiteJobs,
    execute: Callable[[Job], None],
    *,
    analysis_seconds: float,
    qa_seconds: float,
    once: bool = False,
) -> None:
    if root.resolve() != jobs.db.parent.resolve():
        raise ValueError("database and worker lock must share a data root")
    with WorkerLock(root):
        jobs.recover()
        ensure_compute_idle(root)
        while True:
            jobs.apply_queued_cancellations()
            job = jobs.claim(analysis_seconds, qa_seconds)
            if job is not None:
                try:
                    if not job.deadline_at_utc or datetime.fromisoformat(
                        job.deadline_at_utc
                    ) <= datetime.now(UTC):
                        jobs.fail(job, ErrorCode.DEADLINE_EXCEEDED)
                    else:
                        with compute_guard(
                            root, job.job_id, may_spawn=jobs.uses_audio_process(job)
                        ):
                            execute(job)
                        # A completed use case publishes its own terminal state atomically.
                        jobs.fail(job, ErrorCode.STORAGE_ERROR)
                except VideoAgentError as exc:
                    jobs.fail(job, exc.code)
                except OSError, ValueError, LookupError:
                    jobs.fail(job, ErrorCode.STORAGE_ERROR)
            if once:
                return
            if job is None:
                time.sleep(0.25)
