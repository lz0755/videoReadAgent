from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.local_service import client
from tests.integration.test_m2_entrypoints import sources
from video_agent.adapters.storage.connection import connect_sqlite

__all__ = ["client", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_upload_requires_local_authorized_request(client: TestClient) -> None:
    files = {"file": ("sample.mp4", b"unauthorized")}
    client.headers.pop("authorization")
    assert client.post("/v1/assets", files=files).status_code == 401
    client.headers["Authorization"] = "Bearer local-integration-token"
    for headers in [{"Origin": "https://unrelated.example"}, {"Host": "unrelated.example"}]:
        assert client.post("/v1/assets", files=files, headers=headers).status_code == 403
    assert client.post("/v1/assets", files=files).status_code == 422


def test_http_cannot_register_paths_or_echo_private_inputs(
    client: TestClient, tmp_path: Path, sources: dict[str, Path]
) -> None:
    private = str(tmp_path / "private-media.mp4")
    response = client.post("/v1/assets", json={"path": private, "authorization": "local-text"})
    assert response.status_code in (415, 422) and private not in response.text
    response = client.post(
        "/v1/assets",
        files={"file": ("../private-media.mp4", sources["F05"].read_bytes())},
        data={"authorization": "private-auth-receipt"},
    )
    assert response.status_code == 422 and "private-auth-receipt" not in response.text
    assert not (tmp_path / "private-media.mp4").exists()
    with closing(connect_sqlite(tmp_path / "managed" / "agent.sqlite3")) as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone() == (0,)
