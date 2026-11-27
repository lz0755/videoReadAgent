import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.bootstrap import build_jobs, load_app_config
from video_agent.domain.models import Job
from video_agent.worker import run_queue

__all__ = ["client", "sources", "published"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "video_agent.cli", *args, "--data-root", str(root)],
        capture_output=True,
        text=True,
        timeout=40,
        encoding="utf-8",
    )


def test_claim_is_durable_before_computation_and_releases_sql_writer(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    asset, _ = published
    root = tmp_path / "managed"
    job_id = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()[
        "job_id"
    ]
    entered = []

    def inspect(job: Job) -> None:
        with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,attempt_no,started_at_utc,deadline_at_utc FROM jobs WHERE job_id=?",
                (job.job_id,),
            ).fetchone()
            assert row == ("running", 1, job.started_at_utc, job.deadline_at_utc)
            assert job.started_at_utc and job.deadline_at_utc
            conn.rollback()
        entered.append(job.job_id)

    run_queue(
        root,
        build_jobs(root, load_app_config()),
        inspect,
        analysis_seconds=60,
        qa_seconds=20,
        once=True,
    )
    assert entered == [job_id]


def test_worker_consumes_original_queue_ids_once(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    asset, _ = published
    created = client.post(
        f"/v1/assets/{asset}/analyses", json={"profile": "overview", "execution_mode": "offline"}
    ).json()
    result = command(root, "worker", "--offline", "--once")
    assert result.returncode == 0, result.stderr
    job = client.get("/v1/jobs/" + created["job_id"]).json()
    assert job["status"] == "partial" and job["attempt_no"] == 1
    assert datetime.fromisoformat(job["started_at_utc"]) < datetime.fromisoformat(
        job["deadline_at_utc"]
    )
    path = "/v1/analyses/" + created["analysis_id"] + "/questions"
    q = client.post(path, json={"query_text": "画面中有什么？"}).json()
    assert client.get("/v1/jobs/" + q["qa_job_id"]).json()["progress"]["committed_stages"] == {}
    assert command(root, "worker", "--offline", "--once").returncode == 0
    answer = client.get("/v1/questions/" + q["question_id"]).json()
    assert answer["answer"]["question_id"] == q["question_id"]
    assert answer["answer"]["trace_id"] == q["qa_job_id"] and answer["usage"]["total_attempts"] > 0
    again = command(root, "worker", "--offline", "--once")
    assert again.returncode == 0
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM answers WHERE question_id=?", (q["question_id"],)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT attempt_no FROM jobs WHERE job_id=?", (q["qa_job_id"],)
        ).fetchone() == (1,)
