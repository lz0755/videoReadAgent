from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.local_service import client
from tests.integration.test_m2_entrypoints import sources
from video_agent.bootstrap import index_registered_asset
from video_agent.cli import _run_offline_coro

__all__ = ["client", "sources"]


@pytest.fixture
def published(client: TestClient, tmp_path: Path, sources: dict[str, Path]) -> tuple[str, str]:
    response = client.post(
        "/v1/assets",
        files={"file": ("clip.mp4", sources["F05"].read_bytes())},
        data={"authorization": "self-checked F05"},
    )
    assert response.status_code == 201
    asset: str = response.json()["asset_id"]
    result = _run_offline_coro(
        index_registered_asset(
            data_root=tmp_path / "managed",
            asset_id=asset,
            analysis_id=None,
            profile="software_tutorial",
            mode="offline",
            config_path=tmp_path / "managed/config.yaml",
        )
    )
    assert result["index_state"] == "ready"
    return asset, str(result["analysis_id"])
