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
from video_agent.domain.errors import ErrorCode, VideoAgentError

__all__ = ["scene", "stored_window"]


def test_cancelled_job_cannot_be_published(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, files, result = stored_window
    stages = StageCache(db, files)
    stages.commit("w", "key", result)
    with closing(connect_sqlite(db)) as conn:
        conn.execute("UPDATE jobs SET status='cancelled',cancel_requested=1 WHERE job_id='j'")
        with pytest.raises(VideoAgentError) as err:
            SQLiteSearchIndex(db, stages).publish("asset", "a", {"w": "key"})
        assert err.value.code == ErrorCode.JOB_CANCELLED
        assert conn.execute("SELECT status FROM jobs").fetchone() == ("cancelled",)
        assert conn.execute("SELECT COUNT(*) FROM search_fts").fetchone() == (0,)


def test_orphaned_current_fts_row_is_replaced(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, files, result = stored_window
    stages = StageCache(db, files)
    stages.commit("w", "key", result)
    writer = SQLiteSearchIndex(db, stages)
    count = writer.publish("asset", "a", {"w": "key"})
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM search_docs WHERE rowid=(SELECT min(rowid) FROM search_docs)")
        assert writer.publish("asset", "a", {"w": "key"}) == count
        assert conn.execute(
            "SELECT COUNT(*),COUNT(DISTINCT doc_id) FROM search_fts"
        ).fetchone() == (count, count)
