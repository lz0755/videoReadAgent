from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command
from tests.integration.test_job_recovery import crash
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.bootstrap import build_jobs, execute_queued_job, load_app_config
from video_agent.cli import _run_offline_coro
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import Job
from video_agent.domain.ports import VisionRequest, VisionResponse
from video_agent.worker import run_queue

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_lost_frame_after_confirmed_response_requires_consent_before_recomputing(
    client: TestClient, sources: dict[str, Path], tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    asset = client.post(
        "/v1/assets",
        files={"file": ("new.mp4", sources["F05"].read_bytes())},
        data={"authorization": "synthetic F05"},
    ).json()["asset_id"]
    job = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()["job_id"]
    died = crash(root, "before-stage")
    _, err = died.communicate(timeout=30)
    assert died.returncode == 86, err
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        frames = conn.execute(
            "SELECT relative_path FROM artifacts WHERE asset_id=? AND kind='frame'", (asset,)
        ).fetchall()
        first = conn.execute(
            "SELECT attempt_id,status,cost_status FROM usage_records WHERE job_id=?", (job,)
        ).fetchall()
    assert len(first) == len(frames) == 1 and first[0][1] == "succeeded"
    (root / frames[0][0]).unlink()
    assert command(root, "worker", "--offline", "--once").returncode == 0
    assert client.get("/v1/jobs/" + job).json()["error_code"] == "EXTERNAL_RESULT_UNKNOWN"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert (
            conn.execute(
                "SELECT attempt_id,status,cost_status FROM usage_records WHERE job_id=?", (job,)
            ).fetchall()
            == first
        )


def test_lost_cited_frame_cannot_be_returned_as_a_completed_answer(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    _, analysis = published
    q = client.post(f"/v1/analyses/{analysis}/questions", json={"query_text": "observed"}).json()
    assert command(root, "worker", "--offline", "--once").returncode == 0
    query = "/v1/questions/" + q["question_id"]
    result = client.get(query).json()
    eid = result["answer"]["claims"][0]["evidence_ids"][0]
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        path = conn.execute(
            "SELECT relative_path FROM evidence JOIN artifacts USING(asset_id,artifact_id) "
            "WHERE evidence_id=?",
            (eid,),
        ).fetchone()[0]
    (root / path).unlink()
    assert client.get(query).json() == {"status": "failed"}
    assert command(root, "worker", "--offline", "--once").returncode == 0
    assert client.get("/v1/jobs/" + q["qa_job_id"]).json()["error_code"] == "INVALID_CITATION"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM answers WHERE question_id=?", (q["question_id"],)
        ).fetchone() == (1,)


def test_direct_commands_refuse_stale_catalog_and_report_busy_without_paths(
    published: tuple[str, str], sources: dict[str, Path], tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    asset, analysis = published
    slice_args = (
        "slice",
        "--offline",
        "--video",
        str(sources["F05"]),
        "--question",
        "observed",
        "--json",
    )
    marker = root / "compute-active"
    marker.write_text("previous compute", "utf-8")
    busy = command(root, *slice_args)
    assert busy.returncode == 1 and "RESOURCE_BUSY" in busy.stderr
    assert str(Path.cwd()) not in busy.stderr and "Traceback" not in busy.stderr
    marker.unlink()
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        conn.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=2")
    for args in (
        slice_args,
        (
            "ask",
            "--offline",
            "--asset-id",
            asset,
            "--analysis-id",
            analysis,
            "--question",
            "observed",
            "--json",
        ),
    ):
        refused = command(root, *args)
        assert refused.returncode == 1 and "MIGRATION_ERROR" in refused.stderr
        assert "Traceback" not in refused.stderr


def test_cancellation_after_dispatch_keeps_unknown_bill_and_beats_provider_failure(
    client: TestClient, published: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, _ = published
    root = tmp_path / "managed"
    job_id = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()[
        "job_id"
    ]
    jobs = build_jobs(root, load_app_config())

    async def timeout(provider: FakeVisionProvider, request: VisionRequest) -> VisionResponse:
        jobs.cancel(request.job_id)
        raise ProviderError(ErrorCode.PROVIDER_TIMEOUT, "simulated late response")

    def execute(job: Job) -> None:
        _run_offline_coro(
            execute_queued_job(
                jobs,
                job,
                mode="offline",
                config_path=root / "config.yaml",
                budget_config_path=None,
                asr_model_path=None,
            )
        )

    monkeypatch.setattr(FakeVisionProvider, "observe", timeout)
    run_queue(root, jobs, execute, analysis_seconds=120, qa_seconds=60, once=True)
    job = jobs.job(job_id)
    assert (job["status"], job["error_code"]) == ("cancelled", "JOB_CANCELLED")
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute(
            "SELECT status,cost_status FROM usage_records WHERE job_id=?", (job_id,)
        ).fetchall() == [("unknown", "unknown")]
