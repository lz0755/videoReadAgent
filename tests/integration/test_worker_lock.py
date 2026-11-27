import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_job_queue import command
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.bootstrap import build_jobs, load_app_config
from video_agent.domain.errors import VideoAgentError
from video_agent.worker import run_queue


def test_second_process_refused_and_lock_identity_survives_exit(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    run_migrations(root / "agent.sqlite3", data_root=root, writers_stopped=True)
    lock = root / "worker.lock"
    identity = lock.stat().st_ino
    child = subprocess.Popen(
        [sys.executable, "-m", "video_agent.cli", "worker", "--offline", "--data-root", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        until = time.monotonic() + 10
        while time.monotonic() < until and child.poll() is None:
            time.sleep(0.05)
            try:
                if lock.read_text(encoding="utf-8").strip() == f"pid={child.pid}":
                    break
            except PermissionError:
                break
        assert child.poll() is None, "first worker must own the OS lock until stopped"
        begin = time.monotonic()
        second = command(root, "worker", "--offline", "--once")
        assert second.returncode != 0 and "WORKER_ALREADY_RUNNING" in second.stderr
        assert time.monotonic() - begin < 8
        cli = command(
            root, "index", "--asset-id", "unused", "--profile", "overview", "--offline", "--json"
        )
        assert cli.returncode != 0 and "WORKER_ALREADY_RUNNING" in cli.stderr
    finally:
        child.terminate()
        child.communicate(timeout=10)
    with WorkerLock(root):
        assert lock.stat().st_ino == identity


def test_queue_cannot_use_lock_from_another_root(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    run_migrations(root / "agent.sqlite3", data_root=root, writers_stopped=True)
    jobs = build_jobs(root, load_app_config())
    with pytest.raises((ValueError, VideoAgentError)):
        run_queue(
            tmp_path / "other",
            jobs,
            lambda job: pytest.fail("wrong root"),
            analysis_seconds=90,
            qa_seconds=20,
            once=True,
        )
