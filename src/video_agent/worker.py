"""Single-worker process entrypoint (M5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from video_agent.adapters.storage.jobs import SQLiteJobs
from video_agent.domain.models import Job


def run_queue(
    root: Path, jobs: SQLiteJobs, execute: Callable[[Job], None], *,
    analysis_seconds: float, qa_seconds: float, once: bool = False,
) -> None:
    raise NotImplementedError("M5 queue consumer")
