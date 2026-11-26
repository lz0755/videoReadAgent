import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.contract.test_vision_provider import candidate
from tests.integration.test_roi_evidence import scene
from tests.integration.test_stage_cache import stored_window
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.search_index import SQLiteSearchIndex
from video_agent.adapters.storage.stages import StageCache
from video_agent.application.observations import WindowResult, fuse_window
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.tokenizer import TOKENIZER_VERSION

__all__ = ["scene", "stored_window"]


def test_fts_original_tokens_replay_version_and_analysis_isolation(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, files, old = stored_window
    stages = StageCache(db, files)
    writer = SQLiteSearchIndex(db, stages)
    for analysis in ("a", "b"):
        if analysis == "b":
            with closing(connect_sqlite(db)) as conn:
                create_analysis_with_job(
                    conn,
                    analysis_id=analysis,
                    asset_id="asset",
                    job_id="j2",
                    config_hash="config",
                    prompt_bundle_hash="bundle",
                    created_at="now",
                )
        inputs = old.inputs.model_copy(update={"analysis_id": analysis})
        observed = candidate().model_copy(
            update={
                "analysis_id": analysis,
                "evidence_ids": [inputs.frames[0].evidence_id],
                "text": "修改孔径为 20 mm",
            }
        )
        stage = fuse_window(inputs, [observed], old.provenance, originals=old.originals)
        stages.commit("w", "original", stage)
        assert writer.publish("asset", analysis, {"w": "original"}) == 3
    with closing(connect_sqlite(db)) as conn:
        rows = conn.execute(
            "SELECT d.analysis_id,d.original_text,d.tokens,d.index_version FROM search_fts "
            "JOIN search_docs d ON d.doc_id=search_fts.doc_id WHERE search_fts MATCH '孔径' "
            "ORDER BY d.analysis_id"
        ).fetchall()
        assert [r[0] for r in rows] == ["a", "b"]
        assert all(
            r[1] == "修改孔径为 20 mm"
            and "孔径" in r[2].split()
            and "mm" in r[2].split()
            and TOKENIZER_VERSION in r[3]
            for r in rows
        )
        conn.execute(
            "CREATE TRIGGER no_rebuild BEFORE INSERT ON search_docs "
            "BEGIN SELECT RAISE(ABORT,'unexpected rewrite'); END"
        )
        assert writer.publish("asset", "a", {"w": "original"}) == 3
        conn.execute("DROP TRIGGER no_rebuild")
    assert writer.publish("asset", "a", {"w": "original"}, document_version="doc-v2") == 3
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_fts").fetchone() == (6,)
        assert conn.execute(
            "SELECT DISTINCT analysis_id FROM search_docs WHERE index_version LIKE '%doc-v2%'"
        ).fetchall() == [("a",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM stage_results WHERE stage='index' AND status='committed'"
        ).fetchone() == (2,)


def test_index_transaction_failure_and_missing_window_do_not_publish(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, files, result = stored_window
    stages = StageCache(db, files)
    stages.commit("w", "key", result)
    writer = SQLiteSearchIndex(db, stages)
    with pytest.raises(VideoAgentError):
        writer.publish("asset", "a", {"w": "key", "absent": "another"})
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "CREATE TRIGGER stop_ready BEFORE UPDATE OF index_state ON analysis_runs "
            "WHEN NEW.index_state='ready' BEGIN SELECT RAISE(ABORT,'fail publication'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="fail publication"):
            writer.publish("asset", "a", {"w": "key"})
        for table in ("search_docs", "search_fts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM stage_results WHERE stage='index' AND status='committed'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT index_state FROM analysis_runs").fetchone() != ("ready",)
