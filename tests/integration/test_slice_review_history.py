"""Partial CLI review and evidence identity across repeated offline manifest runs."""

import json
from pathlib import Path
from typing import Any

import pytest

from eval import slice_runner
from eval.manifest import load_dev_seed_annotations
from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from video_agent import cli
from video_agent.adapters.llm.fake import FakePlannerProvider
from video_agent.bootstrap import SliceContainer, build_slice_container


def test_cli_retains_successes_and_failed_candidates_in_question_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    video = tmp_path / "f01.mp4"
    command = generate_f01_cfr(video)
    assert self_check_f01_cfr(video, command=command).passed
    planner = FakePlannerProvider(scenarios=("normal", "fabricated_citation", "normal"))

    def compose(**kwargs: Any) -> SliceContainer:
        assert kwargs["mode"] == "offline"
        return build_slice_container(**kwargs, planner_provider=planner)

    monkeypatch.setattr(cli, "build_slice_container", compose)
    args = ["slice", "--video", str(video), "--offline", "--json", "--data-root", str(tmp_path)]
    for question in ("孔径", "孔径", "火星表面气压"):
        args.extend(["--question", question])
    assert cli.main(args) != 0
    payload = json.loads(capsys.readouterr().out)
    first, second, third = payload["questions"]
    assert first["answer"]["status"] == "answered"
    assert second["question_id"] == "q_02" and second["answer"] is None
    assert second["error_code"] == "INVALID_CITATION"
    assert second["qa_usage"]["total_attempts"] == 1
    assert third["answer"]["status"] == "not_answerable"
    trace = json.loads((tmp_path / payload["evidence_manifest"]).read_text(encoding="utf-8"))
    assert trace["question_errors"]["q_02"]["search_hits"]


def test_rerun_keeps_failed_report_and_pins_separate_analysis_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    entry = slice_runner.load_slice_manifest(root / "eval/manifests/slice.jsonl")[0]
    annotations = load_dev_seed_annotations(root / entry["annotations_path"])
    groups = slice_runner.validate_slice_manifest(
        [entry], annotations, repo_root=root, verify_files_on_disk=True
    )
    assert len(groups[entry["video_id"]]) == 4
    manifest = tmp_path / "slice.jsonl"
    manifest.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    planner = FakePlannerProvider(scenarios=("normal", "fabricated_citation", "normal"))

    def compose(**kwargs: Any) -> SliceContainer:
        assert kwargs["mode"] == "offline"
        return build_slice_container(**kwargs, planner_provider=planner)

    monkeypatch.setattr(slice_runner, "build_slice_container", compose)
    out, work = tmp_path / "report", tmp_path / "work"
    argv = [
        "--offline",
        "--manifest",
        str(manifest),
        "--output-dir",
        str(out),
        "--data-root",
        str(work),
        "--max-frames",
        "1",
    ]
    assert slice_runner.main(argv) != 0
    latest = out / "slice_offline_report.json"
    failed_bytes = latest.read_bytes()
    first = json.loads(failed_bytes)
    assert slice_runner.main(argv) == 0
    second = json.loads(latest.read_bytes())
    assert (out / "failed_runs" / first["run_id"] / latest.name).read_bytes() == failed_bytes
    assert first["run_id"] != second["run_id"]
    old_video, new_video = first["videos"][0], second["videos"][0]
    assert old_video["analysis_id"] != new_video["analysis_id"]
    for video_result in (old_video, new_video):
        trace = json.loads((work / video_result["evidence_manifest"]).read_text(encoding="utf-8"))
        assert trace["analysis_id"] == video_result["analysis_id"]
