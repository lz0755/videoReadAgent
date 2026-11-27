import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.integration.test_sqlite_contract import _seed_ready_asset
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.search import SQLiteSearch
from video_agent.application.indexing import DOCUMENT_VERSION
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import TimeRangeMs
from video_agent.domain.tokenizer import TOKENIZER_VERSION, tokenize_to_fts_string


def add_doc(
    conn: sqlite3.Connection, asset: str, analysis: str, seq: int, text: str, start: int
) -> str:
    source = f"{analysis}-{seq:04d}"
    ev = f"e-{source}"
    kind = "observation" if seq == 1 else "transcript"
    conn.execute(
        "INSERT INTO evidence(evidence_id,asset_id,kind,start_ms,end_ms,payload,created_at) "
        "VALUES(?,?,'transcript',?,?,'{}','now')",
        (ev, asset, start, start + 100),
    )
    if kind == "observation":
        conn.execute(
            "INSERT INTO observations VALUES(?,?,?,'heard',?,?,?,NULL,'{}','now')",
            (source, analysis, asset, text, start, start + 100),
        )
        conn.execute("INSERT INTO observation_evidence VALUES(?,?,?)", (source, ev, asset))
    tokens = tokenize_to_fts_string(text)
    conn.execute(
        "INSERT INTO search_docs VALUES(?,?,?,?,?,?,?,?,?,?,'now')",
        (
            source,
            analysis,
            asset,
            kind,
            source if seq == 1 else ev,
            text,
            tokens,
            start,
            start + 100,
            f"{TOKENIZER_VERSION}/{DOCUMENT_VERSION}",
        ),
    )
    conn.execute("INSERT INTO search_fts(doc_id,tokens) VALUES(?,?)", (source, tokens))
    return source


@pytest.fixture
def index(tmp_path: Path) -> Path:
    db = tmp_path / "search.sqlite3"
    run_migrations(db, data_root=tmp_path, writers_stopped=True)
    with closing(connect_sqlite(db)) as conn:
        for asset in ("one", "other"):
            _seed_ready_asset(conn, asset, "source-" + asset)
        for asset, analysis in (("one", "A"), ("one", "B"), ("other", "C")):
            create_analysis_with_job(
                conn,
                asset_id=asset,
                analysis_id=analysis,
                job_id="j-" + analysis,
                config_hash="cfg",
                prompt_bundle_hash="bundle",
                created_at="now",
            )
            add_doc(conn, asset, analysis, 1, "修改孔径为 20 mm", 3000)
            add_doc(conn, asset, analysis, 2, "拉伸", 3100)
            for seq in range(3, 11):
                add_doc(conn, asset, analysis, seq, "孔径", 3200 + seq)
            conn.execute(
                "INSERT INTO stage_results(analysis_id,asset_id,stage,item_key,config_hash,"
                "checksum,status,created_at) "
                "VALUES(?,?,'index','all','cfg','sha','committed','now')",
                (analysis, asset),
            )
        conn.execute("UPDATE analysis_runs SET index_state='ready',status='partial'")
        # Fixture self check uses the actual FTS table independently of the reader.
        assert conn.execute(
            "SELECT count(*) FROM search_fts WHERE search_fts MATCH '改孔'"
        ).fetchone() == (3,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return db


def test_first_retrieval_recall_ranking_scope_and_provenance(index: Path) -> None:
    reader = SQLiteSearch(index)
    gold = {
        "孔径": {f"A-{n:04d}" for n in (1, *range(3, 11))},
        "修改孔径": {"A-0001"},
        "拉伸": {"A-0002"},
    }
    recalled = 0
    for query, expected in gold.items():
        hits = reader.search("one", "A", query)
        assert 0 < len(hits) <= 5
        assert all(h.asset_id == "one" and h.analysis_id == "A" for h in hits)
        recalled += bool(expected.intersection(h.doc_id for h in hits))
    assert recalled == len(gold)
    top = reader.search("one", "A", "修改孔径")[0]
    assert (top.source_kind, top.source_id, top.evidence_ids, top.text) == (
        "observation",
        "A-0001",
        ["e-A-0001"],
        "修改孔径为 20 mm",
    )
    assert top.time_range == TimeRangeMs(start_ms=3000, end_ms=3100)
    assert reader.search("one", "A", "拉伸")[0].evidence_ids == ["e-A-0002"]
    assert len(reader.search("one", "A", "孔径", limit=10)) == 9
    assert reader.search("one", "A", "不存在") == []


def test_half_open_and_single_character_scope(index: Path) -> None:
    reader = SQLiteSearch(index)
    window = TimeRangeMs(start_ms=3099, end_ms=3100)
    assert [h.doc_id for h in reader.search("one", "A", "孔", time_range=window)] == ["A-0001"]
    assert reader.search("one", "A", "拉伸", time_range=window) == []
    assert (
        reader.search("one", "A", "孔径", time_range=TimeRangeMs(start_ms=3100, end_ms=3200)) == []
    )
    with closing(connect_sqlite(index)) as conn:
        conn.execute("BEGIN")
        for seq in range(11, 502):
            add_doc(conn, "one", "B", seq, "占位", 4000)
        conn.commit()
    assert reader.search("one", "A", "孔")
    with pytest.raises(VideoAgentError) as truncated:
        reader.search("one", "B", "孔")
    assert truncated.value.details.get("scan_limit") == 500


@pytest.mark.parametrize("query", ['孔径" OR (拉伸)', "mm' --", "NEAR(孔径 拉伸)", '") OR ('])
def test_fts_punctuation_is_text(index: Path, query: str) -> None:
    reader = SQLiteSearch(index)
    try:
        hits = reader.search("one", "A", query)
    except VideoAgentError as exc:
        assert exc.code == ErrorCode.SCHEMA_VALIDATION_FAILED
    else:
        assert hits == reader.search("one", "A", tokenize_to_fts_string(query))
        assert all(h.analysis_id == "A" and h.asset_id == "one" for h in hits)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE analysis_runs SET index_state='indexing' WHERE analysis_id='A'",
        "UPDATE search_docs SET index_version='old' WHERE doc_id='A-0002'",
        "DELETE FROM stage_results WHERE analysis_id='A'",
    ],
)
def test_unready_or_stale_index_is_explicit(index: Path, change: str) -> None:
    with closing(connect_sqlite(index)) as conn:
        conn.execute(change)
    with pytest.raises(VideoAgentError) as exc:
        SQLiteSearch(index).search("one", "A", "孔径")
    assert exc.value.code == ErrorCode.INDEX_NOT_READY


@pytest.mark.parametrize("limit", [0, 11, True])
def test_invalid_limits(index: Path, limit: int) -> None:
    with pytest.raises(VideoAgentError) as exc:
        SQLiteSearch(index).search("one", "A", "孔径", limit=limit)
    assert exc.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED
