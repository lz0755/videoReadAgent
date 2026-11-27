import json
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command
from tests.integration.test_job_recovery import crash
from video_agent.adapters.storage.connection import connect_sqlite

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_retry_explains_possible_second_charge_before_confirmation(
    client: TestClient, published: tuple[str, str], tmp_path: Path
) -> None:
    _, analysis = published
    root = tmp_path / "managed"
    ids = client.post(f"/v1/analyses/{analysis}/questions", json={"query_text": "observed"}).json()
    proc = crash(root, "pending")
    proc.communicate(timeout=30)
    assert proc.returncode == 86
    assert command(root, "worker", "--offline", "--once").returncode == 0
    job_id = ids["qa_job_id"]
    job_path = f"/v1/jobs/{job_id}"
    before = client.get(job_path).json()
    assert before["status"] == "failed" and before["error_code"] == "EXTERNAL_RESULT_UNKNOWN"

    def attempts() -> list[tuple[str, str, str]]:
        with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
            return conn.execute(
                "SELECT attempt_id,status,cost_status FROM usage_records "
                "WHERE job_id=? ORDER BY rowid",
                (job_id,),
            ).fetchall()

    original = attempts()
    assert len(original) == 1 and original[0][2] == "unknown"
    refused = command(root, "jobs", "retry", job_id)
    assert refused.returncode != 0
    assert json.loads(refused.stderr)["error"]["code"] == "EXTERNAL_RESULT_UNKNOWN"
    notice = refused.stderr.lower()
    assert "charg" in notice and "--confirm-external-risk" in notice
    assert any(word in notice for word in ("twice", "again", "additional", "another", "duplicate"))
    assert client.get(job_path).json() == before
    assert attempts() == original
