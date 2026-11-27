import json
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command
from tests.integration.test_job_recovery import crash, times
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.api.app import create_app
from video_agent.domain.errors import ErrorCode, VideoAgentError

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


@pytest.mark.parametrize("cancelled", [False, True])
def test_pending_qa_retains_usage_and_original_question_on_restart(
    client: TestClient, published: tuple[str, str], tmp_path: Path, cancelled: bool
) -> None:
    _, analysis = published
    root = tmp_path / "managed"
    ids = client.post(
        f"/v1/analyses/{analysis}/questions", json={"query_text": "画面中有什么？"}
    ).json()
    q, j = ids["question_id"], ids["qa_job_id"]
    proc = crash(root, "pending")
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 86, err
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        original = conn.execute(
            "SELECT attempt_id,cost_status,reservation_json FROM usage_records WHERE job_id=?", (j,)
        ).fetchall()
    assert (
        len(original) == 1
        and original[0][1] == "unknown"
        and json.loads(original[0][2])["dispatched"]
    )
    before = times(root, j)
    if cancelled:
        assert client.post(f"/v1/jobs/{j}/cancel").json()["cancel_requested"]
    assert command(root, "worker", "--offline", "--once").returncode == 0
    job = client.get(f"/v1/jobs/{j}").json()
    assert (job["status"], job["error_code"]) == (
        ("cancelled", "JOB_CANCELLED") if cancelled else ("failed", "EXTERNAL_RESULT_UNKNOWN")
    )
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert (
            conn.execute(
                "SELECT attempt_id,cost_status,reservation_json FROM usage_records WHERE job_id=?",
                (j,),
            ).fetchall()
            == original
        )
    if not cancelled:
        assert command(root, "jobs", "retry", j).returncode != 0
        assert command(root, "jobs", "retry", j, "--confirm-external-risk").returncode == 0
        assert command(root, "worker", "--offline", "--once").returncode == 0
        answer = client.get(f"/v1/questions/{q}").json()
        assert answer["answer"]["question_id"] == q and answer["usage"]["total_attempts"] > 1
        assert answer["usage"]["cost_status"] == "unknown"
    assert times(root, j) == before


def test_expired_job_cannot_gain_time_on_restart(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    asset, _ = published
    root = tmp_path / "managed"
    j = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()["job_id"]
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        conn.execute(
            "UPDATE jobs SET status='running',started_at_utc='2020-01-01T00:00:00+00:00',"
            "deadline_at_utc='2020-01-01T01:00:00+00:00' WHERE job_id=?",
            (j,),
        )
    before = times(root, j)
    assert command(root, "worker", "--offline", "--once").returncode == 0
    assert client.get(f"/v1/jobs/{j}").json()["error_code"] == "DEADLINE_EXCEEDED"
    assert times(root, j) == before


def test_missing_committed_frame_invalidates_published_index(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    asset, analysis = published
    root = tmp_path / "managed"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        path = conn.execute(
            "SELECT relative_path FROM artifacts WHERE asset_id=? AND kind='frame' LIMIT 1",
            (asset,),
        ).fetchone()[0]
    (root / path).unlink()
    assert command(root, "worker", "--offline", "--once").returncode == 0
    view = client.get(f"/v1/analyses/{analysis}").json()
    assert view["index_state"] != "ready" and view["status"] == "failed"


def test_existing_cli_and_queued_qa_obey_same_local_guards(
    client: TestClient, published: tuple[str, str], sources: dict[str, Path], tmp_path: Path
) -> None:
    _, analysis = published
    root = tmp_path / "managed"
    with WorkerLock(root):
        p = command(
            root,
            "slice",
            "--offline",
            "--video",
            str(sources["F05"]),
            "--question",
            "frame?",
            "--json",
        )
        assert p.returncode != 0 and "WORKER_ALREADY_RUNNING" in p.stderr
    config = root / "config.yaml"
    cfg = yaml.safe_load(config.read_text("utf-8"))
    cfg["media"]["max_file_bytes"] = 1
    config.write_text(yaml.safe_dump(cfg), "utf-8")
    ids = client.post(f"/v1/analyses/{analysis}/questions", json={"query_text": "frame?"}).json()
    assert command(root, "worker", "--offline", "--once", "--config", str(config)).returncode == 0
    j = client.get("/v1/jobs/" + ids["qa_job_id"]).json()
    assert j["error_code"] == "MEDIA_LIMIT_EXCEEDED" and j["progress"]["model_attempts"] == 0


def test_opening_stale_catalog_does_not_migrate_under_service(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        conn.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=2")
    with pytest.raises(VideoAgentError) as failure:
        create_app(root, token="local-test")
    assert failure.value.code is ErrorCode.MIGRATION_ERROR
    assert "MIGRATION_ERROR" in command(root, "worker", "--offline", "--once").stderr
