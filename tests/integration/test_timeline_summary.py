import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.integration.test_roi_evidence import scene
from tests.integration.test_stage_cache import stored_window
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.search_index import SQLiteSearchIndex
from video_agent.adapters.storage.stages import StageCache
from video_agent.application.observations import WindowResult

__all__ = ["scene", "stored_window"]


def test_committed_summary_resolves_original_sources_gaps_and_repairs_without_new_observations(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, files, result = stored_window
    assert result.gaps and {o.kind for o in result.observations} == {"observed", "heard"}
    stages = StageCache(db, files)
    stages.commit("w", "key", result)
    writer = SQLiteSearchIndex(db, stages)
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "CREATE TRIGGER refuse_publish BEFORE UPDATE OF index_state ON analysis_runs "
            "WHEN NEW.index_state='ready' BEGIN SELECT RAISE(ABORT,'refuse'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="refuse"):
            writer.publish("asset", "a", {"w": "key"})
        read = (
            "SELECT artifact_id,checksum FROM stage_results WHERE stage='summarize' "
            "AND analysis_id='a' AND asset_id='asset' AND status='committed'"
        )
        assert not conn.execute(read).fetchall()
        conn.execute("DROP TRIGGER refuse_publish")
        assert writer.publish("asset", "a", {"w": "key"}) == 3
        rows = conn.execute(read).fetchall()
        assert len(rows) == 1
        aid, checksum = rows[0]
        blob = files.read_bytes(aid)
        assert hashlib.sha256(blob).hexdigest() == checksum
        summary = json.loads(blob)
        assert (summary["analysis_id"], summary["asset_id"]) == ("a", "asset")
        assert summary["visual_coverage_gaps"] == [r.model_dump() for r in result.gaps]
        assert {entry["kind"] for entry in summary["entries"]} == {"heard", "observed"}
        for entry in summary["entries"]:
            source = next(
                o for o in result.observations if o.observation_id == entry["observation_id"]
            )
            assert (entry["text"], entry["evidence_ids"], entry["time_range"]) == (
                source.text,
                source.evidence_ids,
                source.time_range.model_dump(),
            )
        assert writer.publish("asset", "a", {"w": "key"}) == 3
        assert conn.execute(read).fetchall() == rows
        files.resolve_path(aid).write_bytes(b"broken")
        assert writer.publish("asset", "a", {"w": "key"}) == 3
        restored = conn.execute(read).fetchone()
        assert files.read_bytes(restored[0]) == blob and restored[1] == checksum
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (2,)
        assert stages.load("asset", "a", "w", "other-key") is None
        assert not conn.execute(read).fetchall()
