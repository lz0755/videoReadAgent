"""Local ASGI client over a freshly migrated catalog, with a bounded test config."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from video_agent.adapters.storage.migrate import run_migrations
from video_agent.api.app import create_app

LOCAL = "http://127.0.0.1:8000"
TOKEN = "local-integration-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def service(root: Path, *, max_bytes: int = 524_288_000) -> TestClient:
    root.mkdir(exist_ok=True)
    run_migrations(root / "agent.sqlite3", data_root=root, writers_stopped=True)
    config = yaml.safe_load(Path("config/defaults.yaml").read_text(encoding="utf-8"))
    config["media"]["max_file_bytes"] = max_bytes
    settings = root / "config.yaml"
    settings.write_text(yaml.safe_dump(config), encoding="utf-8")
    return TestClient(create_app(root, token=TOKEN, config_path=settings), base_url=LOCAL)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with service(tmp_path / "managed") as session:
        session.headers.update(AUTH)
        yield session
