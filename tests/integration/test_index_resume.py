import json
from contextlib import closing
from pathlib import Path

import pytest

from tests.integration.test_m2_entrypoints import sources
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.cli import main

__all__ = ["sources"]


@pytest.mark.parametrize("reason", ["profile", "unknown", "expired", "missing_frame"])
def test_resume_preserves_identity_deadline_and_published_index(
    reason: str,
    tmp_path: Path,
    sources: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "store"
    assert (
        main(
            [
                "assets",
                "import",
                str(sources["F05"]),
                "--data-root",
                str(root),
                "--authorization",
                "fixture",
                "--json",
            ]
        )
        == 0
    )
    asset = json.loads(capsys.readouterr().out)["asset_id"]
    common = ["index", "--asset-id", asset, "--offline", "--json", "--data-root", str(root)]
    args = [*common, "--profile", "software_tutorial"]
    assert main(args) == 0
    analysis = json.loads(capsys.readouterr().out)["analysis_id"]
    db = root / "agent.sqlite3"
    with closing(connect_sqlite(db)) as conn:
        original = conn.execute(
            "SELECT config_hash,index_state,status FROM analysis_runs"
        ).fetchone()
        assert original[1] == "ready"
        if reason == "missing_frame":
            aid, rel = conn.execute(
                "SELECT artifact_id,relative_path FROM artifacts WHERE kind='frame'"
            ).fetchone()
            frame = root / rel
            body = frame.read_bytes()
            frame.write_bytes(bytes([body[0] ^ 1]) + body[1:])
            assert main([*args, "--analysis-id", analysis]) == 0
            assert conn.execute("SELECT COUNT(*) FROM usage_records").fetchone() == (2,)
            assert conn.execute("SELECT index_state FROM analysis_runs").fetchone() == ("ready",)
            current = conn.execute(
                "SELECT DISTINCT e.artifact_id FROM search_docs d JOIN "
                "observation_evidence oe ON oe.observation_id=d.source_id JOIN evidence e "
                "ON e.evidence_id=oe.evidence_id WHERE e.kind='frame'"
            ).fetchall()
            assert current and all(row[0] != aid for row in current)
            return
        if reason == "profile":
            operation = [*common, "--profile", "overview", "--analysis-id", analysis]
        elif reason == "unknown":
            operation = [*args, "--analysis-id", "never-created", "--retry"]
        else:
            conn.execute("UPDATE budget_accounts SET deadline_at_utc='2000-01-01T00:00:00Z'")
            operation = [*args, "--analysis-id", analysis]
        assert main(operation) != 0
        assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone() == (1,)
        assert (
            conn.execute("SELECT config_hash,index_state,status FROM analysis_runs").fetchone()
            == original
        )
        assert conn.execute("SELECT COUNT(*) FROM usage_records").fetchone() == (1,)
