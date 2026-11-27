import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.worker_lock import WorkerLock

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def crash(root: Path, fault: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.fixtures.worker_faults", str(root), fault],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def times(root: Path, job: str) -> tuple[str, str]:
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        result: tuple[str, str] = conn.execute(
            "SELECT started_at_utc,deadline_at_utc FROM jobs WHERE job_id=?", (job,)
        ).fetchone()
    return result


@pytest.mark.parametrize("point", ["before-stage", "after-stage"])
def test_real_exit_on_either_side_of_stage_commit(
    client: TestClient, published: tuple[str, str], tmp_path: Path, point: str
) -> None:
    asset, _ = published
    root = tmp_path / "managed"
    ids = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()
    analysis, job = ids["analysis_id"], ids["job_id"]
    proc = crash(root, point)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 86, err

    def snapshot() -> tuple[list[tuple[str]], list[tuple[str]]]:
        with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
            return (
                conn.execute(
                    "SELECT checksum FROM stage_results WHERE stage='observe' AND "
                    "analysis_id=? AND status='committed'",
                    (analysis,),
                ).fetchall(),
                conn.execute(
                    "SELECT attempt_id FROM usage_records WHERE job_id=? ORDER BY rowid", (job,)
                ).fetchall(),
            )

    stages, usage = snapshot()
    assert len(usage) == 1 and len(stages) == int(point == "after-stage")
    original = times(root, job)
    assert client.get(f"/v1/jobs/{job}").json()["status"] == "running"
    assert command(root, "worker", "--offline", "--once").returncode == 0
    state = client.get(f"/v1/jobs/{job}").json()
    assert snapshot() == (stages, usage)
    if point == "after-stage":
        assert state["status"] == "partial" and state["attempt_no"] == 2
    else:
        assert state["error_code"] == "EXTERNAL_RESULT_UNKNOWN" and state["status"] == "failed"
        assert command(root, "jobs", "retry", job).returncode != 0
        assert command(root, "jobs", "retry", job, "--confirm-external-risk").returncode == 0
        assert command(root, "worker", "--offline", "--once").returncode == 0
        assert client.get(f"/v1/jobs/{job}").json()["status"] == "partial"
        assert len(snapshot()[1]) == 2 and snapshot()[1][0] == usage[0]
    assert times(root, job) == original


def test_running_cancel_beats_window_commit_and_new_sends(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    asset, _ = published
    root = tmp_path / "managed"
    ids = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()
    proc = crash(root, "cancel")
    try:
        for _ in range(200):
            if (root / "at-stage").exists() or proc.poll() is not None:
                break
            time.sleep(0.05)
        assert (root / "at-stage").exists()
        assert client.post("/v1/jobs/" + ids["job_id"] + "/cancel").json()["cancel_requested"]
    finally:
        (root / "resume-stage").touch()
        proc.communicate(timeout=15)
    assert client.get("/v1/jobs/" + ids["job_id"]).json()["status"] == "cancelled"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM stage_results WHERE analysis_id=? AND stage='observe'",
            (ids["analysis_id"],),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM usage_records WHERE job_id=?", (ids["job_id"],)
        ).fetchone() == (1,)


def test_parent_exit_with_living_compute_child_blocks_replacement(
    client: TestClient, sources: dict[str, Path], tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    asset = client.post(
        "/v1/assets",
        files={"file": ("audio.mp4", sources["F04"].read_bytes())},
        data={"authorization": "synthetic F04"},
    ).json()["asset_id"]
    ids = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()
    proc = crash(root, "orphan")
    try:
        proc.communicate(timeout=15)
        assert proc.returncode == 86 and (root / "child-ready").exists()
        with WorkerLock(root):
            assert not (root / "child-done").exists()
        blocked = command(root, "worker", "--offline", "--once")
        assert blocked.returncode != 0 and "RESOURCE_BUSY" in blocked.stderr
        with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
            assert conn.execute(
                "SELECT count(*) FROM stage_results WHERE analysis_id=?", (ids["analysis_id"],)
            ).fetchone() == (0,)
    finally:
        (root / "stop-child").touch()
        for _ in range(100):
            if (root / "child-done").exists():
                break
            time.sleep(0.05)
    assert (root / "child-done").exists()
