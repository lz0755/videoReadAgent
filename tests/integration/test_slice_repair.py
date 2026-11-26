"""Offline M1.5 acceptance through the CLI and manifest runner, using real media."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval import slice_runner
from eval.manifest import load_dev_seed_annotations
from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from tests.unit.test_budget_guard import FakeClock
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.bootstrap import SliceContainer, build_slice_container
from video_agent.cli import main as cli_main
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import FrameEvidence, Observation, TranscriptSegment
from video_agent.domain.ports import SearchHit, VisionRequest, VisionResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    entries = slice_runner.load_slice_manifest(REPO_ROOT / "eval/manifests/slice.jsonl")[:2]
    annotations = load_dev_seed_annotations(REPO_ROOT / entries[0]["annotations_path"])
    grouped = slice_runner.validate_slice_manifest(
        entries,
        annotations,
        repo_root=REPO_ROOT,
        verify_files_on_disk=True,
    )
    assert [len(grouped[e["video_id"]]) for e in entries] == [4, 4]
    path = tmp_path / "slice.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def inject_ports(
    monkeypatch: pytest.MonkeyPatch,
    vision: FakeVisionProvider,
    planner: FakePlannerProvider,
    clock: FakeClock | None = None,
) -> None:
    def compose(**kwargs: Any) -> SliceContainer:
        assert kwargs["mode"] == "offline"
        return build_slice_container(
            **kwargs,
            vision_provider=vision,
            planner_provider=planner,
            clock=clock,
        )

    monkeypatch.setattr(slice_runner, "build_slice_container", compose)


def run_report(tmp_path: Path, manifest: Path, budget: Path | None = None) -> tuple[int, Any]:
    argv = [
        "--manifest",
        str(manifest),
        "--offline",
        "--data-root",
        str(tmp_path / "work"),
        "--output-dir",
        str(tmp_path / "report"),
        "--max-frames",
        "1",
    ]
    if budget is not None:
        argv.extend(["--budget-config", str(budget)])
    status = slice_runner.main(argv)
    return status, json.loads(
        (tmp_path / "report" / "slice_offline_report.json").read_text(encoding="utf-8")
    )


def budget_file(tmp_path: Path, **limits: Any) -> Path:
    path = tmp_path / "budget.yaml"
    baseline = {"max_model_attempts": 12, "max_submitted_image_parts": 4, "deadline_seconds": 60}
    path.write_text(
        yaml.safe_dump(
            {
                "live_enabled": False,
                "cloud_egress_authorized": False,
                "budget": baseline | limits,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_second_qa_failure_preserves_prior_and_later_pinned_results(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = FakePlannerProvider(scenarios=("normal", "fabricated_citation", "normal"))
    inject_ports(monkeypatch, FakeVisionProvider(), planner)
    status, report = run_report(tmp_path, manifest)
    pinned = [qid for e in slice_runner.load_slice_manifest(manifest) for qid in e["question_ids"]]
    rows = report["questions"]
    assert status != 0
    assert [q["question_id"] for q in rows] == pinned
    assert [q["execution_status"] for q in rows] == ["completed", "failed"] + ["completed"] * 6
    assert rows[1]["error_code"] == "INVALID_CITATION"
    assert rows[1]["answer_status"] is None
    assert any(
        c["question_id"] == pinned[1] and c["error_code"] == "INVALID_CITATION"
        for c in report["failed_cases"]
    )
    assert len(planner.calls) == 8
    assert report["usage_totals"]["provider_attempts"] == 10
    assert report["diagnostic_gate"]["decision"] == "pause"


@pytest.mark.parametrize("role", ["vision", "planner"])
def test_unknown_paid_attempt_keeps_its_reservation_and_all_question_outcomes(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    vision = FakeVisionProvider()
    planner = FakePlannerProvider()
    if role == "vision":
        vision = FakeVisionProvider(scenarios=("unknown_result", "normal"))
        vision.model_id = "deepseek-flash"
    else:
        planner = FakePlannerProvider(scenarios=("unknown_result", "normal"))
        planner.model_id = "deepseek-flash"
    inject_ports(monkeypatch, vision, planner)
    status, report = run_report(tmp_path, manifest)
    assert status != 0
    rows = report["questions"]
    assert len(rows) == 8
    if role == "vision":
        assert all(r["execution_status"] == "not_run" for r in rows[:4])
    else:
        assert rows[0]["execution_status"] == "failed"
    assert rows[0]["error_code"] == "EXTERNAL_RESULT_UNKNOWN"
    assert all(r["execution_status"] == "completed" for r in rows[4:])
    totals = report["usage_totals"]
    assert totals["provider_attempts"] == len(vision.calls) + len(planner.calls)
    assert totals["unknown_cost_events"] == 1
    assert totals["cost_status"] == "unknown"
    assert totals["estimated_cost_usd"] > 0


@pytest.mark.parametrize(
    ("limits", "vision_calls", "qa_calls"),
    [
        ({"max_model_attempts": 3}, 1, 2),
        ({"max_submitted_image_parts": 1}, 1, 4),
        ({"max_estimated_cost_usd": 0.0}, 0, 0),
    ],
)
def test_numeric_approval_applies_before_dispatch_across_questions_and_videos(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: dict[str, Any],
    vision_calls: int,
    qa_calls: int,
) -> None:
    vision, planner = FakeVisionProvider(), FakePlannerProvider()
    vision.model_id = "deepseek-flash"
    inject_ports(monkeypatch, vision, planner)
    status, report = run_report(tmp_path, manifest, budget_file(tmp_path, **limits))
    assert (len(vision.calls), len(planner.calls)) == (vision_calls, qa_calls)
    assert status != 0
    assert report["usage_totals"]["provider_attempts"] == vision_calls + qa_calls
    assert len(report["questions"]) == 8
    assert any(q.get("error_code") == "BUDGET_EXCEEDED" for q in report["questions"])


def test_new_qa_scope_does_not_restart_approved_deadline(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()

    class SlowVision(FakeVisionProvider):
        async def observe(self, request: VisionRequest) -> VisionResponse:
            response = await super().observe(request)
            clock.advance(3)
            return response

    vision, planner = SlowVision(), FakePlannerProvider()
    inject_ports(monkeypatch, vision, planner, clock)
    status, report = run_report(
        tmp_path,
        manifest,
        budget_file(tmp_path, deadline_seconds=3, per_request_timeout_seconds=2),
    )
    assert len(vision.calls) == 1 and not planner.calls
    assert vision.calls[0].timeout_seconds == 2
    assert status != 0
    assert all(q["error_code"] == "DEADLINE_EXCEEDED" for q in report["questions"])


def test_invalid_numeric_budget_rejected_before_any_model_call(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision, planner = FakeVisionProvider(), FakePlannerProvider()
    inject_ports(monkeypatch, vision, planner)
    with pytest.raises(ValueError):
        run_report(tmp_path, manifest, budget_file(tmp_path, max_model_attempts=True))
    assert not vision.calls and not planner.calls


def test_textual_consent_is_rejected_at_preflight_without_credentials(
    tmp_path: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = budget_file(tmp_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config.update(live_enabled=True, cloud_egress_authorized="false")
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises((ValueError, ProviderError)) as exc:
        slice_runner.run_slice_manifest(
            manifest_path=manifest,
            mode="live",
            data_root=tmp_path / "work",
            output_dir=tmp_path / "out",
            budget_config_path=path,
        )
    if isinstance(exc.value, ProviderError):
        assert exc.value.code is ErrorCode.CLOUD_DISABLED


def test_cli_evidence_can_be_reopened_and_resolved_after_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    video = tmp_path / "f01.mp4"
    command = generate_f01_cfr(video)
    assert self_check_f01_cfr(video, command=command).passed
    work = tmp_path / "work"
    assert (
        cli_main(
            [
                "slice",
                "--video",
                str(video),
                "--data-root",
                str(work),
                "--offline",
                "--json",
                "--question",
                "孔径",
                "--question",
                "火星表面气压",
            ]
        )
        == 0
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    pointer = Path(payload["evidence_manifest"])
    assert not pointer.is_absolute() and ".." not in pointer.parts
    trace = json.loads((work / pointer).read_text(encoding="utf-8"))
    assert (
        trace["asset_id"] == payload["asset_id"] and trace["analysis_id"] == payload["analysis_id"]
    )
    assert trace["provider_mode"] == "offline_fake"
    assert trace["audio_artifact_id"] and list(work.rglob(trace["audio_artifact_id"] + ".wav"))
    frames = [FrameEvidence.model_validate(f) for f in trace["frames"]]
    segments = [TranscriptSegment.model_validate(s) for s in trace["transcript_segments"]]
    observations = [Observation.model_validate(o) for o in trace["observations"]]
    assert len(frames) == len(payload["frames"]) and frames
    assert len(segments) == payload["transcript_segment_count"] and segments
    assert len(observations) == payload["observation_count"] and observations
    assert any("孔径" in seg.text for seg in segments)
    sources = {s.evidence_id for s in segments} | {f.evidence_id for f in frames}
    for frame in frames:
        assert list(work.rglob(frame.artifact_id + ".jpg"))
    for obs in observations:
        assert set(obs.evidence_ids) <= sources and obs.analysis_id == trace["analysis_id"]
    questions = trace["questions"]
    hits = [SearchHit.model_validate(h) for h in questions[0]["search_hits"]]
    assert hits and len(hits) == payload["questions"][0]["search_hit_count"]
    assert all(
        h.asset_id == trace["asset_id"]
        and h.analysis_id == trace["analysis_id"]
        and set(h.evidence_ids) <= sources
        for h in hits
    )
    assert questions[1]["search_hits"] == []
    assert [q["question_id"] for q in questions] == [q["question_id"] for q in payload["questions"]]
