import shutil
from contextlib import closing
from pathlib import Path

from tests.integration.test_chinese_search import add_doc
from tests.integration.test_sqlite_contract import _seed_ready_asset
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.migrate import run_migrations


def test_existing_v1_documents_gain_scoped_access_without_republication(tmp_path: Path) -> None:
    baseline = tmp_path / "v1"
    baseline.mkdir()
    first = Path("src/video_agent/adapters/storage/migrations/001_initial.sql")
    shutil.copyfile(first, baseline / first.name)
    db = tmp_path / "published.sqlite3"
    assert run_migrations(
        db, data_root=tmp_path, writers_stopped=True, migrations_dir=baseline
    ) == [1]
    with closing(connect_sqlite(db)) as conn:
        _seed_ready_asset(conn, "one", "source")
        create_analysis_with_job(
            conn,
            asset_id="one",
            analysis_id="A",
            job_id="job",
            config_hash="cfg",
            prompt_bundle_hash="p",
            created_at="now",
        )
        add_doc(conn, "one", "A", 1, "孔径", 300)
        before = conn.execute("SELECT * FROM search_docs").fetchall()
    assert run_migrations(db, data_root=tmp_path, writers_stopped=True) == [2]
    with closing(connect_sqlite(db)) as conn:
        assert before == conn.execute("SELECT * FROM search_docs").fetchall()
        assert conn.execute(
            "SELECT doc_id FROM search_fts WHERE search_fts MATCH '孔径'"
        ).fetchall() == [("A-0001",)]
        for projection in ("original_text", "DISTINCT index_version", "1"):
            steps = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT {projection} FROM search_docs "
                "WHERE asset_id=? AND analysis_id=? LIMIT 501",
                ("one", "A"),
            ).fetchall()
            assert any(
                "SEARCH search_docs USING" in row[3] and "analysis_id=?" in row[3] for row in steps
            )
