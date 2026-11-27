import hashlib
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message

from tests.fixtures.local_service import AUTH, LOCAL, service
from tests.integration.test_m2_entrypoints import sources
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.worker_lock import WorkerLock

__all__ = ["sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_exact_file_limit_during_worker_lifetime(tmp_path: Path, sources: dict[str, Path]) -> None:
    raw = sources["F05"].read_bytes()
    root = tmp_path / "managed"
    with service(root, max_bytes=len(raw)) as client, WorkerLock(root):
        for size, status in [(len(raw) + 1, 413), (len(raw), 201)]:
            response = client.post(
                "/v1/assets",
                files={"file": ("clip.mp4", (raw + b"x")[:size])},
                data={"authorization": "synthetic F05"},
                headers={**AUTH, "Content-Length": "1"},
            )
            assert response.status_code == status
        asset = response.json()["asset_id"]
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute("SELECT asset_id,sha256,size_bytes FROM assets").fetchall() == [
            (asset, hashlib.sha256(raw).hexdigest(), len(raw))
        ]
    assert not list(root.rglob("*.part"))


def raw_request(client: TestClient, chunks: list[Message]) -> tuple[int, int]:
    messages: list[Message] = []
    consumed = 0

    async def receive() -> Message:
        nonlocal consumed
        consumed += 1
        return chunks.pop(0)

    async def send(message: Message) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/assets",
        "raw_path": b"/v1/assets",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 12000),
        "server": ("127.0.0.1", 8000),
        "headers": [
            (b"host", LOCAL.removeprefix("http://").encode()),
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-type", b"multipart/form-data; boundary=b"),
        ],
    }
    assert client.portal is not None
    client.portal.call(client.app, scope, receive, send)
    return messages[0]["status"], consumed


@pytest.mark.parametrize("disconnect", [True, False])
def test_incomplete_multipart_never_registers(
    tmp_path: Path, sources: dict[str, Path], disconnect: bool
) -> None:
    root = tmp_path / "managed"
    raw = sources["F05"].read_bytes()
    body = (
        b'--b\r\nContent-Disposition: form-data; name="authorization"\r\n\r\ntest\r\n'
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="ok.mp4"\r\n\r\n'
        + raw
        + b"\r\n--b\r\n"
    )
    with service(root) as client:
        messages: list[Message] = [{"type": "http.request", "body": body, "more_body": disconnect}]
        if disconnect:
            messages.append({"type": "http.disconnect"})
        status, _ = raw_request(client, messages)
        assert 400 <= status < 500
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute("SELECT count(*) FROM assets").fetchone() == (0,)
    assert not list(root.rglob("*.part"))


def test_total_body_limit_stops_receiving_and_cleans(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    with service(root, max_bytes=1024) as client:
        status, consumed = raw_request(
            client,
            [
                {"type": "http.request", "body": b"x" * (1024 + 65536 + 1), "more_body": True},
                {"type": "http.request", "body": b"", "more_body": False},
            ],
        )
        assert (status, consumed) == (413, 1)
    assert not list(root.rglob("*.part"))


def test_body_limit_accumulates_across_receives(tmp_path: Path, sources: dict[str, Path]) -> None:
    raw = sources["F05"].read_bytes()
    body = (
        b'--b\r\nContent-Disposition: form-data; name="authorization"\r\n\r\nfixture\r\n'
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\n\r\n'
        + raw
        + b"\r\n--b--\r\n"
    )
    root = tmp_path / "managed"
    with service(root, max_bytes=len(raw)) as client:
        status, consumed = raw_request(
            client,
            [
                {"type": "http.request", "body": body, "more_body": True},
                {"type": "http.request", "body": b" " * 65536, "more_body": True},
                {"type": "http.request", "body": b"", "more_body": False},
            ],
        )
    assert (status, consumed) == (413, 2)
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute("SELECT count(*) FROM assets").fetchone() == (0,)
