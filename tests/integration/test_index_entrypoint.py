import json
from contextlib import closing
from pathlib import Path

import pytest

from tests.integration.test_m2_entrypoints import sources
from video_agent.adapters.llm.fake import FakeVisionProvider
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.cli import main
from video_agent.domain.ports import VisionRequest, VisionResponse

__all__ = ["sources"]


def test_registered_video_to_durable_index_and_reopened_cache(
    tmp_path: Path,
    sources: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    observe = FakeVisionProvider.observe

    async def tracked(provider: FakeVisionProvider, request: VisionRequest) -> VisionResponse:
        calls.append(request.attempt_id)
        return await observe(provider, request)

    monkeypatch.setattr(FakeVisionProvider, "observe", tracked)
    data = tmp_path / "managed"
    assert (
        main(
            [
                "assets",
                "import",
                str(sources["F05"]),
                "--authorization",
                "synthetic-F05",
                "--data-root",
                str(data),
                "--json",
            ]
        )
        == 0
    )
    asset = json.loads(capsys.readouterr().out)["asset_id"]
    args = [
        "index",
        "--asset-id",
        asset,
        "--data-root",
        str(data),
        "--offline",
        "--profile",
        "software_tutorial",
        "--json",
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    analysis = first["analysis_id"]
    assert first["index_state"] == "ready" and first["provider_mode"] == "offline_fake"
    assert first["provider_attempts"] == 1 and first["submitted_image_parts"] == 1
    assert first["indexed_documents"] >= 1 and first["audio_status"] == "no_audio"
    with closing(connect_sqlite(data / "agent.sqlite3")) as conn:
        snapshot = conn.execute("SELECT attempt_id FROM usage_records").fetchall()
        products = conn.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id").fetchall()
    disk = {p for p in (data / "artifacts").rglob("*") if p.is_file()}
    assert disk and len(calls) == 1
    assert main(args + ["--analysis-id", analysis]) == 0
    assert disk == {p for p in (data / "artifacts").rglob("*") if p.is_file()}
    assert len(calls) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["analysis_id"] == analysis and second["windows_reused"] == 1
    with closing(connect_sqlite(data / "agent.sqlite3")) as conn:
        assert conn.execute("SELECT attempt_id FROM usage_records").fetchall() == snapshot
        assert (
            conn.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id").fetchall()
            == products
        )
        assert conn.execute("SELECT count(*) FROM search_fts").fetchone() == (
            first["indexed_documents"],
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
