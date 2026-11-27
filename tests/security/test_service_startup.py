from pathlib import Path

import pytest

from video_agent.adapters.storage.migrate import run_migrations
from video_agent.api.app import create_app


@pytest.mark.parametrize("token", ["", "private-startup-token\x7f"])
def test_api_requires_valid_token_before_startup(tmp_path: Path, token: str) -> None:
    root = tmp_path / "managed"
    run_migrations(root / "agent.sqlite3", data_root=root, writers_stopped=True)
    with pytest.raises(ValueError) as refused:
        create_app(root, token=token)
    assert "private-startup-token" not in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
