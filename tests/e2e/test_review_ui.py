import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from tests.fixtures.local_service import TOKEN
from tests.fixtures.published_service import client, published, sources
from tests.integration.test_job_queue import command

__all__ = ["client", "published", "sources"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


@pytest.fixture
def api_url(tmp_path: Path, published: tuple[str, str]) -> Iterator[str]:
    root = tmp_path / "managed"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "video_agent.cli",
            "serve",
            "--data-root",
            str(root),
            "--port",
            str(port),
        ],
        env={**os.environ, "VIDEO_AGENT_TOKEN": TOKEN},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(trust_env=False, timeout=2) as probe:
            for _ in range(200):
                try:
                    if probe.get(url + "/v1/jobs/ready").status_code == 401:
                        break
                except httpx.ConnectError:
                    pass
                assert server.poll() is None
                time.sleep(0.025)
            else:
                pytest.fail("local API did not start")
        yield url
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_review_page_uses_real_api_and_preview_fallback(
    api_url: str, published: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_AGENT_API_URL", api_url)
    monkeypatch.setenv("VIDEO_AGENT_TOKEN", TOKEN)
    page = AppTest.from_file(
        str(Path("src/video_agent/ui/app.py").resolve()), default_timeout=15
    ).run()
    assert not page.exception
    assert page.get("file_uploader")[0].proto.max_upload_size_mb == 64
    assert "CLI" in " ".join(c.value for c in page.caption)
    asset, analysis = published
    page.text_input(key="asset_id").set_value(asset).run()
    page.text_input(key="analysis_id").set_value(analysis).run()
    assert page.get("image") and len(page.get("video")) == 0
    assert "实际" in " ".join(c.value for c in page.caption)
    page.text_input(key="question_text").set_value("画面中有什么？")
    page.button(key="ask").click().run()
    assert "queued" in " ".join(c.value for c in page.info)
    assert command(tmp_path / "managed", "worker", "--offline", "--once").returncode == 0
    page.run()
    assert not page.exception and not page.error
    assert page.session_state["question_id"]
    visible = " ".join(c.value for c in [*page.markdown, *page.caption, *page.info, *page.warning])
    with httpx.Client(
        base_url=api_url, headers={"Authorization": "Bearer " + TOKEN}, trust_env=False
    ) as http:
        answer = http.get("/v1/questions/" + page.session_state["question_id"]).json()["answer"]
    assert answer["status"] in visible
    assert any(
        line in visible for line in [*answer["gaps"], *(c["text"] for c in answer["claims"])]
    )
    assert TOKEN not in visible and str(tmp_path) not in visible
