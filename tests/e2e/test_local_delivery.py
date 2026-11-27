import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command
from video_agent.adapters.storage.connection import connect_sqlite

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_answer_to_registered_frame_and_cli_exports(
    client: TestClient, published: tuple[str, str], tmp_path: Path, sources: dict[str, Path]
) -> None:
    root = tmp_path / "managed"
    asset, analysis = published
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'observed'"
            ).fetchone()[0]
            >= 1
        )
    submitted = command(
        root,
        "questions",
        "create",
        "--analysis-id",
        analysis,
        "--text",
        "observed",
        "--idempotency-key",
        "local-e2e",
    )
    assert submitted.returncode == 0
    question = json.loads(submitted.stdout)
    assert command(root, "worker", "--offline", "--once").returncode == 0
    qid = question["question_id"]
    answer = client.get("/v1/questions/" + qid).json()["answer"]
    assert answer["question_id"] == qid and answer["claims"]
    cite = answer["claims"][0]["evidence_ids"][0]
    meta = client.get("/v1/evidence/" + cite, params={"analysis_id": analysis}).json()["evidence"]
    frame = client.get(
        "/v1/evidence/" + cite, params={"analysis_id": analysis, "representation": "frame"}
    )
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        data = conn.execute(
            "SELECT sha256,relative_path FROM artifacts WHERE asset_id=? AND artifact_id=?",
            (asset, meta["artifact_id"]),
        ).fetchone()
    assert (
        frame.content == (root / data[1]).read_bytes()
        and hashlib.sha256(frame.content).hexdigest() == data[0]
    )
    assert (
        command(root, "jobs", "retry", question["qa_job_id"], "--confirm-external-risk").returncode
        != 0
    )
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        (cost_status,) = conn.execute(
            "SELECT DISTINCT cost_status FROM usage_records WHERE scope_id=?", (analysis,)
        ).fetchone()
    for fmt, filename in [("json", "review.json"), ("markdown", "review.md")]:
        output = tmp_path / filename
        result = command(
            root, "export", "--analysis-id", analysis, "--format", fmt, "--output", str(output)
        )
        assert result.returncode == 0, result.stderr
        text = output.read_text("utf-8")
        assert str(root) not in text and analysis in text and cite in text and qid in text
        assert hashlib.sha256(sources["F05"].read_bytes()).hexdigest() in text
        assert "fake-vision" in text and "no_audio" in text
        assert cost_status in text
        if fmt == "json":
            document = json.loads(text)
            assert document["schema_version"] == "1.1" and document["analysis_id"] == analysis
            assert (
                document["versions"]["prompt_bundle_hash"] and document["versions"]["config_hash"]
            )
            assert document["usage"]["cost_status"] == cost_status
            assert document["evidence"][cite]["actual_pts_ms"] == meta["actual_pts_ms"]
            assert (
                document["questions"][0]["answer"] == answer
                and document["usage"]["total_attempts"] > 0
            )
