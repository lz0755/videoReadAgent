from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from video_agent.adapters.storage.connection import connect_sqlite

__all__ = ["client", "sources", "published"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_async_submission_is_idempotent_and_questions_survive_reopen(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    asset, ready = published
    headers = {"Idempotency-Key": "intent-1"}
    path = f"/v1/assets/{asset}/analyses"
    payload = {"profile": "overview", "execution_mode": "offline"}
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 202
    job = response.json()["job_id"]
    assert client.post(path, headers=headers, json=payload).json() == response.json()
    assert (
        client.post(
            path, headers=headers, json={**payload, "profile": "software_tutorial"}
        ).status_code
        == 409
    )
    assert client.get(f"/v1/jobs/{job}").json()["status"] == "queued"
    assert client.post(f"/v1/jobs/{job}/cancel").json()["cancel_requested"] is True
    assert client.get(f"/v1/analyses/{response.json()['analysis_id']}").json()["timeline"] == []
    path = f"/v1/analyses/{ready}/questions"
    q = {"query_text": "画面中有什么？", "mode": "qa"}
    first = client.post(path, headers=headers, json=q)
    assert first.status_code == 202
    ids = first.json()
    assert client.post(path, headers=headers, json=q).json() == ids
    assert (
        client.post(path, headers=headers, json={**q, "query_text": "别的问题"}).status_code == 409
    )
    assert client.get(f"/v1/questions/{ids['question_id']}").json() == {"status": "queued"}
    with closing(connect_sqlite(tmp_path / "managed/agent.sqlite3")) as conn:
        assert conn.execute(
            "SELECT question_id,job_id,analysis_id FROM jobs WHERE kind='qa'"
        ).fetchall() == [(ids["question_id"], ids["qa_job_id"], ready)]
        assert conn.execute("SELECT query_text FROM questions").fetchall() == [(q["query_text"],)]
        assert conn.execute("SELECT count(*) FROM answers").fetchone() == (0,)
    assert client.get("/v1/jobs/missing").status_code == 404
    assert client.post("/v1/analyses/missing/questions", json=q).status_code == 404


def test_timeline_page_uses_real_observations_and_bounds(
    client: TestClient, published: tuple[str, str]
) -> None:
    asset, analysis = published
    path = f"/v1/analyses/{analysis}"
    first = client.get(path, params={"offset": 0, "limit": 1})
    assert first.status_code == 200
    page = first.json()
    assert page["asset_id"] == asset and page["status"] == "succeeded"
    (entry,) = page["timeline"]
    assert entry["kind"] == "observed" and entry["evidence_ids"]
    assert entry not in client.get(path, params={"offset": 1, "limit": 1}).json()["timeline"]
    assert client.get(path, params={"start_ms": 9000}).json()["timeline"] == []
    assert client.get(path, params={"limit": 0}).status_code == 422
    assert client.get(path, params={"start_ms": 200, "end_ms": 100}).status_code == 422
