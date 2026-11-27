import hashlib
import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_event_localization import QUERY, Clip, clip, make_rig
from tests.integration.test_m2_entrypoints import sources
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.qa import SQLiteQA
from video_agent.adapters.storage.search import SQLiteSearch
from video_agent.cli import main
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import FrameEvidence, Question, TimeRangeMs
from video_agent.domain.ports import (
    PlannerRequest,
    PlannerResponse,
    ToolCallProposal,
    VisionRequest,
    VisionResponse,
)

__all__ = ["clip", "sources"]


@pytest.fixture
def indexed(
    tmp_path: Path, sources: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> tuple[Path, str, str]:
    root = tmp_path / "managed"
    args = ["--data-root", str(root), "--json"]
    assert main(["assets", "import", str(sources["F05"]), "--authorization", "fixture", *args]) == 0
    asset = json.loads(capsys.readouterr().out)["asset_id"]
    assert main(["index", "--asset-id", asset, "--offline", "--profile", "overview", *args]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["index_state"] == "ready"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'observed'"
            ).fetchone()[0]
            >= 1
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return root, asset, indexed["analysis_id"]


def ask(
    indexed: tuple[Path, str, str], query: str, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any]]:
    root, asset, analysis = indexed
    code = main(
        [
            "ask",
            "--asset-id",
            asset,
            "--analysis-id",
            analysis,
            "--question",
            query,
            "--data-root",
            str(root),
            "--offline",
            "--json",
            *args,
        ]
    )
    output = capsys.readouterr()
    return code, json.loads(output.err if code else output.out)


def test_published_answer_uses_one_counted_first_search_and_durable_job(
    indexed: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, asset, analysis = indexed
    for query, status in [("observed", "answered"), ("绝不匹配", "not_answerable")]:
        code, result = ask(indexed, query, capsys)
        assert code == 0 and result["provider_mode"] == "offline_fake"
        assert result["answer"]["status"] == status
        if status == "answered":
            ids = result["first_hits"][0]["evidence_ids"]
            assert result["answer"]["claims"][0]["evidence_ids"][0] in ids
        else:
            assert result["first_hits"] == [] and result["answer"]["gaps"]
        with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
            row = conn.execute(
                "SELECT q.query_text,j.job_id,a.payload,b.consumed_json FROM questions q "
                "JOIN jobs j USING(question_id,asset_id,analysis_id) "
                "JOIN answers a USING(question_id,asset_id,analysis_id) "
                "JOIN budget_accounts b ON b.scope_id=q.question_id WHERE q.question_id=? "
                "AND q.asset_id=? AND q.analysis_id=? AND j.status='succeeded'",
                (result["question_id"], asset, analysis),
            ).fetchone()
            assert row is not None and row[:2] == (query, result["qa_job_id"])
            assert json.loads(row[2]) == result["answer"]
            counters = json.loads(row[3])
            assert (counters["tool_calls"], counters["provider_attempts"]) == (1, 1)
            assert conn.execute(
                "SELECT scope_id,job_id FROM usage_records WHERE scope_id=?",
                (result["question_id"],),
            ).fetchall() == [(result["question_id"], result["qa_job_id"])]


def test_unready_index_and_event_gap_have_distinct_outcomes(
    indexed: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, analysis = indexed
    code, result = ask(indexed, "observed", capsys, "--mode", "event_localization")
    assert code == 0 and result["first_hits"]
    assert result["answer"]["status"] != "answered" and result["answer"]["gaps"]
    assert result["answer"]["localization"] is None
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        conn.execute(
            "UPDATE analysis_runs SET index_state='pending' WHERE analysis_id=?", (analysis,)
        )
    code, result = ask(indexed, "observed", capsys)
    assert code == 1 and result["error"]["code"] == ErrorCode.INDEX_NOT_READY
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        assert conn.execute("SELECT count(*) FROM answers").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM jobs WHERE kind='qa' AND status='failed' AND error_code=?",
            (ErrorCode.INDEX_NOT_READY,),
        ).fetchone() == (1,)


def test_reader_excludes_another_run_of_the_same_asset(indexed: tuple[Path, str, str]) -> None:
    root, asset, analysis = indexed
    db = root / "agent.sqlite3"
    question = Question(
        question_id="test_q",
        analysis_id=analysis,
        asset_id=asset,
        query_text="observed",
        created_at_utc="now",
    )
    hit = SQLiteSearch(db).search(asset, analysis, "observed")[0]
    reader = SQLiteQA(db, question)
    frame = reader.resolve(hit.evidence_ids[0])
    assert isinstance(frame, FrameEvidence) and frame.actual_pts_ms == hit.time_range.start_ms
    assert reader.read_observation(hit).evidence_ids == hit.evidence_ids
    assert [f.evidence_id for f in reader.context(hit)] == hit.evidence_ids
    with closing(connect_sqlite(db)) as conn:
        create_analysis_with_job(
            conn,
            analysis_id="other",
            asset_id=asset,
            job_id="other-job",
            config_hash="cfg",
            prompt_bundle_hash="bundle",
            created_at="now",
        )
    foreign = SQLiteQA(db, question.model_copy(update={"analysis_id": "other"}))
    assert foreign.resolve(frame.evidence_id) is None
    assert not foreign.allow_region("r1c1", TimeRangeMs(start_ms=0, end_ms=1))
    with pytest.raises(VideoAgentError):
        foreign.read_observation(hit)
    assert not foreign.context(hit)


def test_qa_review_dispatches_real_frames_and_charges_nested_vision(
    indexed: tuple[Path, str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, _ = indexed
    original = FakePlannerProvider.decide
    calls = 0

    async def inspect_once(
        provider: FakePlannerProvider, request: PlannerRequest
    ) -> PlannerResponse:
        nonlocal calls
        decision = await original(provider, request)
        calls += 1
        if calls == 1:
            return decision.model_copy(
                update={
                    "action": "call_tools",
                    "answer": None,
                    "tool_calls": [
                        ToolCallProposal(
                            tool_name="inspect_clip",
                            tool_call_id="review",
                            arguments={
                                "start_ms": 0,
                                "end_ms": 1000,
                            },
                        )
                    ],
                }
            )
        return decision

    monkeypatch.setattr(FakePlannerProvider, "decide", inspect_once)
    code, result = ask(indexed, "observed", capsys)
    assert code == 0 and calls == 2 and result["answer"]["status"] == "answered"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        rows = conn.execute(
            "SELECT model_id,parent_tool_call_id,json_extract(usage_json,'$.images_sent') "
            "FROM usage_records "
            "WHERE scope_id=? ORDER BY rowid",
            (result["question_id"],),
        ).fetchall()
        assert [r[0] for r in rows] == ["fake-planner", "fake-vision", "fake-planner"]
        assert rows[1][1] and rows[1][2] == 2
        counters = json.loads(
            conn.execute(
                "SELECT consumed_json FROM budget_accounts WHERE scope_id=?",
                (result["question_id"],),
            ).fetchone()[0]
        )
        assert (counters["tool_calls"], counters["expensive_tool_calls"]) == (2, 1)


def test_cancellation_during_dispatch_retains_usage_and_supersedes_answer(
    indexed: tuple[Path, str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, _ = indexed
    db = root / "agent.sqlite3"
    original = FakePlannerProvider.decide
    calls: list[str] = []

    async def cancel(provider: FakePlannerProvider, request: PlannerRequest) -> PlannerResponse:
        calls.append(request.job_id)
        with closing(connect_sqlite(db)) as conn:
            conn.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (request.job_id,))
        return await original(provider, request)

    monkeypatch.setattr(FakePlannerProvider, "decide", cancel)
    code, result = ask(indexed, "observed", capsys)
    assert code == 1 and result["error"]["code"] == ErrorCode.JOB_CANCELLED and len(calls) == 1
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (calls[0],)).fetchone() == (
            "cancelled",
        )
        assert conn.execute("SELECT count(*) FROM answers").fetchone() == (0,)
        assert conn.execute(
            "SELECT status FROM usage_records WHERE job_id=?", (calls[0],)
        ).fetchall() == [("succeeded",)]


def test_operator_ceiling_reserves_last_attempt_for_synthesis(
    indexed: tuple[Path, str, str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root, _, _ = indexed
    approval = tmp_path / "offline-limit.yaml"
    approval.write_text("budget:\n  max_provider_attempts: 1\n", encoding="utf-8")
    code, result = ask(indexed, "observed", capsys, "--budget-config", str(approval))
    assert code == 0 and result["answer"]["status"] == "answered"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        row = conn.execute(
            "SELECT limits_json,consumed_json FROM budget_accounts WHERE scope_id=?",
            (result["question_id"],),
        ).fetchone()
        assert json.loads(row[0])["max_provider_attempts"] == 1
        assert json.loads(row[1])["provider_attempts"] == 1


def test_published_event_uses_scoped_sql_neighbors_and_four_actual_boundary_frames(
    clip: Clip, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clip, tmp_path)

    async def classify(_: FakeVisionProvider, req: VisionRequest) -> VisionResponse:
        return await rig.vision.observe(req)

    monkeypatch.setattr(FakeVisionProvider, "observe", classify)
    code, result = ask((tmp_path, "asset", "a"), QUERY, capsys, "--mode", "event_localization")
    assert code == 0 and result["answer"]["status"] == "answered"
    assert result["first_hits"][0]["doc_id"] == rig.initial.hits[0].doc_id
    event = result["answer"]["localization"]
    assert event["refined"] and len(rig.vision.calls) == 2
    with closing(connect_sqlite(rig.db)) as conn:
        for name, states in [("start_bracket", [False, True]), ("end_bracket", [True, False])]:
            bracket = event[name]
            for prefix, bound, visible in zip(
                ("before", "after"), ("lower", "upper"), states, strict=True
            ):
                row = conn.execute(
                    "SELECT e.point_ms FROM evidence e JOIN observation_evidence l "
                    "USING(evidence_id,asset_id) JOIN observations o "
                    "USING(observation_id,asset_id) "
                    "WHERE e.evidence_id=? AND o.asset_id=? AND o.analysis_id=?",
                    (bracket[prefix + "_evidence_id"], "asset", "a"),
                ).fetchone()
                assert row == (bracket[bound + "_ms"],) and clip.states[row[0]] is visible
        job = conn.execute(
            "SELECT j.status,a.payload FROM jobs j JOIN answers a "
            "USING(question_id,asset_id,analysis_id) "
            "WHERE j.job_id=?",
            (result["qa_job_id"],),
        ).fetchone()
        assert job[0] == "succeeded" and json.loads(job[1]) == result["answer"]
    assert result["consumed"]["tool_calls"] == 3
    assert result["consumed"]["expensive_tool_calls"] == result["usage"]["total_attempts"] == 2
    assert (
        result["usage"]["total_images_sent"] == sum(len(r.images) for r in rig.vision.calls) <= 32
    )


@pytest.mark.parametrize("damage", ["remove", "same_size_change"])
def test_unplayable_cited_frame_cannot_support_a_published_answer(
    indexed: tuple[Path, str, str], capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    root, asset, analysis = indexed
    db = root / "agent.sqlite3"
    hit = SQLiteSearch(db).search(asset, analysis, "observed")[0]
    with closing(connect_sqlite(db)) as conn:
        artifact_id = conn.execute(
            "SELECT artifact_id FROM evidence WHERE evidence_id=? AND asset_id=?",
            (hit.evidence_ids[0], asset),
        ).fetchone()[0]
    files = CatalogArtifactStore(root)
    path = files.resolve_path(artifact_id)
    original = path.read_bytes()
    assert original and files.verify_hash(artifact_id, hashlib.sha256(original).hexdigest())
    if damage == "remove":
        path.unlink()
    else:
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        assert path.stat().st_size == len(original)
    code, result = ask(indexed, "observed", capsys)
    assert code == 0 and result["answer"]["status"] == "not_answerable"
    assert (
        result["answer"]["claims"] == [] and ErrorCode.INVALID_CITATION in result["answer"]["gaps"]
    )
    with closing(connect_sqlite(db)) as conn:
        published = conn.execute(
            "SELECT payload FROM answers WHERE question_id=?", (result["question_id"],)
        ).fetchone()
        assert json.loads(published[0]) == result["answer"]

    path.write_bytes(original)
    unrelated = files.put_bytes(asset, "frame", original, suffix=".jpg")
    files.resolve_path(unrelated).unlink()
    code, restored = ask(indexed, "observed", capsys)
    assert code == 0 and restored["answer"]["status"] == "answered"
