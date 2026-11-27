"""Explicit timeline questions on a published M3 index and decoded frame timestamps."""

import subprocess
from contextlib import closing

import pytest

from tests.contract.test_vision_provider import inputs_for
from tests.integration.test_observation_pipeline import provenance
from video_agent.adapters.media.pyav_decoder import extract_frames
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.search import SQLiteSearch
from video_agent.adapters.storage.search_index import SQLiteSearchIndex
from video_agent.adapters.storage.stages import StageCache
from video_agent.application.observations import fuse_window
from video_agent.application.vision_validation import WindowInputs
from video_agent.bootstrap import build_asset_importer, load_app_config
from video_agent.domain.models import Observation, TimeRangeMs


@pytest.fixture(scope="module")
def timed_index(tmp_path_factory: pytest.TempPathFactory) -> SQLiteSearch:
    root = tmp_path_factory.mktemp("timed-search")
    clip = root / "clock.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=4:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-threads",
            "1",
            str(clip),
        ],
        check=True,
        timeout=45,
    )
    db = root / "agent.sqlite3"
    run_migrations(db, data_root=root, writers_stopped=True)
    files = CatalogArtifactStore(root)
    cache = StageCache(db, files)
    publisher = SQLiteSearchIndex(db, cache)
    importer = build_asset_importer(root, load_app_config())
    for owner, analyses in (("one", ("A", "B")), ("two", ("C",))):
        asset = importer.import_file(clip, asset_id=owner, authorization="generated test clip")
        span = TimeRangeMs(start_ms=0, end_ms=asset.duration_ms)
        frames = extract_frames(files, asset, [925, 4800, 8400], range_ms=span)
        assert [f.actual_pts_ms for f in frames] == [1000, 5000, 8500]
        frames = [
            f.model_copy(update={"evidence_id": f"{owner}-frame-{f.actual_pts_ms}"}) for f in frames
        ]
        for analysis in analyses:
            with closing(connect_sqlite(db)) as conn:
                create_analysis_with_job(
                    conn,
                    asset_id=owner,
                    analysis_id=analysis,
                    job_id="j" + analysis,
                    config_hash="cfg",
                    prompt_bundle_hash="bundle",
                    created_at="now",
                )
            speech = [
                inputs_for(owner, analysis)
                .transcripts[0]
                .model_copy(
                    update={
                        "evidence_id": f"{owner}-speech-{point}",
                        "text": words,
                        "time_range": TimeRangeMs(start_ms=point - 90, end_ms=point + 240),
                    }
                )
                for point, words in [(1000, "blue"), (8500, "silver")]
            ]
            views = [
                Observation(
                    observation_id="candidate",
                    asset_id=owner,
                    analysis_id=analysis,
                    kind="observed",
                    text=label,
                    evidence_ids=[frame.evidence_id],
                    time_range=(
                        span
                        if frame.actual_pts_ms == 5000
                        else TimeRangeMs(
                            start_ms=frame.actual_pts_ms, end_ms=frame.actual_pts_ms + 1
                        )
                    ),
                )
                for frame, label in zip(frames, ("gear", "等待 wait", "valve"), strict=True)
            ]
            inputs = WindowInputs(
                asset_id=owner, analysis_id=analysis, window=span, frames=frames, transcripts=speech
            )
            cache.commit("all", "k", fuse_window(inputs, views, provenance()))
            # Three visual sources, two original transcripts and their two heard observations.
            assert publisher.publish(owner, analysis, {"all": "k"}) == 7
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute(
            "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'valve'"
        ).fetchone() == (3,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return SQLiteSearch(db)


@pytest.mark.parametrize(
    "locator", ["在1秒和8.5秒", "between 1 and 8.5 seconds", "from 8.5s to 1s"]
)
def test_range_covers_both_real_views_and_original_utterances(
    timed_index: SQLiteSearch, locator: str
) -> None:
    hits = timed_index.search("one", "A", locator, limit=4)
    assert {(hit.source_kind, tuple(hit.evidence_ids)) for hit in hits} == {
        ("observation", ("one-frame-1000",)),
        ("observation", ("one-frame-8500",)),
        ("transcript", ("one-speech-1000",)),
        ("transcript", ("one-speech-8500",)),
    }
    assert all(hit.asset_id == "one" and hit.analysis_id == "A" for hit in hits)
    first_two = timed_index.search("one", "A", locator, limit=2)
    assert len(first_two) == 2 and first_two[0].time_range.end_ms < first_two[1].time_range.start_ms


def test_half_open_filter_excludes_frame_at_end_and_distant_lexical_hit(
    timed_index: SQLiteSearch,
) -> None:
    hits = timed_index.search(
        "one",
        "A",
        "valve at 8.5 seconds",
        limit=2,
        time_range=TimeRangeMs(start_ms=8400, end_ms=8500),
    )
    assert [(h.source_kind, h.evidence_ids) for h in hits] == [("transcript", ["one-speech-8500"])]
    assert timed_index.search("one", "A", "valve near 30 seconds") == []
    assert (
        timed_index.search(
            "one", "A", "valve near 8.5 seconds", time_range=TimeRangeMs(start_ms=0, end_ms=500)
        )
        == []
    )


@pytest.mark.parametrize("query", ["在1.25秒", "what shade at 1.25 seconds?"])
def test_single_point_uses_actual_nearby_evidence_without_word_overlap(
    timed_index: SQLiteSearch, query: str
) -> None:
    hits = timed_index.search("one", "A", query, limit=2)
    assert {(h.source_kind, tuple(h.evidence_ids)) for h in hits} == {
        ("transcript", ("one-speech-1000",)),
        ("observation", ("one-frame-1000",)),
    }


@pytest.mark.parametrize("query", ["Does the person wait around 80 seconds?", "是否在80秒内等待？"])
def test_duration_quantities_preserve_plain_lexical_search(
    timed_index: SQLiteSearch, query: str
) -> None:
    hits = timed_index.search("one", "A", query)
    assert [h.evidence_ids for h in hits] == [["one-frame-5000"]]
