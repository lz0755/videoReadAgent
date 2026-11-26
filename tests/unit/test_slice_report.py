"""Unit and contract tests for M1.5.3 slice manifest, diagnostic gates, and runner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from eval.manifest import load_dev_seed_annotations
from eval.slice_runner import (
    SLICE_REPORT_SCHEMA_VERSION,
    SliceReportError,
    evaluate_slice_report,
    load_slice_manifest,
    run_slice_manifest,
    validate_slice_manifest,
)
from eval.slice_runner import (
    main as runner_main,
)
from video_agent.domain.errors import ErrorCode, ProviderError

REPO_ROOT = Path(__file__).resolve().parents[2]
SLICE_MANIFEST_PATH = REPO_ROOT / "eval" / "manifests" / "slice.jsonl"
DEV_ANNOTATIONS_PATH = REPO_ROOT / "eval" / "annotations" / "dev_seed" / "dev_seed_questions.json"
PRIVATE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)")


def _make_live_report(
    manifest_entries: list[dict[str, Any]],
    annotations: dict[str, Any],
    *,
    answerable_correct_count: int = 7,
    unanswerable_abstained_count: int = 3,
    invalid_citation_on_first: bool = False,
    execution_mode: str = "live",
    is_live_quality_evidence: bool = True,
) -> dict[str, Any]:
    q_by_id = {q["question_id"]: q for q in annotations["questions"]}
    questions_out: list[dict[str, Any]] = []
    ans_seen = 0
    unans_seen = 0

    for entry in manifest_entries:
        vid = entry["video_id"]
        for qid in entry["question_ids"]:
            q_meta = q_by_id[qid]
            answerability = q_meta["answerability"]
            if answerability == "answerable":
                is_correct = ans_seen < answerable_correct_count
                ans_seen += 1
                bad_cite = invalid_citation_on_first and ans_seen == 1
                questions_out.append(
                    {
                        "video_id": vid,
                        "question_id": qid,
                        "answerability": answerability,
                        "provider_mode": execution_mode,
                        "answer_status": "answered",
                        "claims_count": 1,
                        "gaps_count": 0,
                        "total_citations": 1,
                        "valid_citations": 0 if bad_cite else 1,
                        "manual_judgment": "correct" if is_correct else "incorrect",
                    }
                )
            else:
                is_abstained = unans_seen < unanswerable_abstained_count
                unans_seen += 1
                questions_out.append(
                    {
                        "video_id": vid,
                        "question_id": qid,
                        "answerability": answerability,
                        "provider_mode": execution_mode,
                        "answer_status": "not_answerable" if is_abstained else "answered",
                        "claims_count": 0 if is_abstained else 1,
                        "gaps_count": 1 if is_abstained else 0,
                        "total_citations": 0 if is_abstained else 1,
                        "valid_citations": 0 if is_abstained else 1,
                        "manual_judgment": "abstained_ok" if is_abstained else "hallucinated",
                    }
                )

    return {
        "schema_version": SLICE_REPORT_SCHEMA_VERSION,
        "execution_mode": execution_mode,
        "is_live_quality_evidence": is_live_quality_evidence,
        "videos": [{"video_id": e["video_id"]} for e in manifest_entries],
        "questions": questions_out,
    }


def test_slice_manifest_valid_and_matches_d0_annotations() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)
    grouped = validate_slice_manifest(entries, annotations, repo_root=REPO_ROOT)

    assert len(entries) == 3
    assert [e["video_id"] for e in entries] == ["vg_pr10_start", "ocr_941", "ocr_750"]
    assert sum(len(qs) for qs in grouped.values()) == 12
    for vid, qs in grouped.items():
        assert len(qs) == 4, f"video {vid} must pin exactly 4 questions"


def test_slice_report_forbids_dropping_or_reclassifying_failed_questions() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)

    # 1. Dropping a single question must raise SliceReportError.
    dropped_report = _make_live_report(entries, annotations)
    dropped_report["questions"] = dropped_report["questions"][:-1]
    with pytest.raises(SliceReportError):
        evaluate_slice_report(dropped_report, manifest_entries=entries, annotations=annotations)

    # 2. Reclassifying an answerable question to not_answerable after the fact must fail.
    reclassified = _make_live_report(entries, annotations)
    for item in reclassified["questions"]:
        if item["answerability"] == "answerable":
            item["answerability"] = "not_answerable"
            break
    with pytest.raises(SliceReportError):
        evaluate_slice_report(reclassified, manifest_entries=entries, annotations=annotations)


def test_slice_report_enforces_fake_vs_live_separation() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)

    # 1. Fake report claiming is_live_quality_evidence=True must be rejected.
    dishonest_fake = _make_live_report(
        entries,
        annotations,
        execution_mode="offline_fake",
        is_live_quality_evidence=True,
    )
    with pytest.raises(SliceReportError):
        evaluate_slice_report(dishonest_fake, manifest_entries=entries, annotations=annotations)

    # 2. Mixing provider_mode inside a live report must be rejected.
    mixed = _make_live_report(entries, annotations, execution_mode="live")
    mixed["questions"][0]["provider_mode"] = "offline_fake"
    with pytest.raises(SliceReportError):
        evaluate_slice_report(mixed, manifest_entries=entries, annotations=annotations)

    # 3. Honest offline_fake report NEVER claims model quality or 'continue'.
    honest_fake = _make_live_report(
        entries,
        annotations,
        answerable_correct_count=9,
        unanswerable_abstained_count=3,
        execution_mode="offline_fake",
        is_live_quality_evidence=False,
    )
    summary = evaluate_slice_report(honest_fake, manifest_entries=entries, annotations=annotations)
    assert summary.execution_mode == "offline_fake"
    assert summary.is_live_quality_evidence is False
    assert summary.can_claim_model_quality is False
    assert summary.quality_gate_passed is False
    assert summary.decision == "pause"


def test_slice_diagnostic_gate_thresholds_and_failure_retention() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)

    # 7/9 answerable correct (ceil(9 * 0.75) == 7), 3/3 abstained, 100% citations -> continue,
    # while STILL retaining the 2 failed questions in failed_question_ids.
    passing = _make_live_report(
        entries,
        annotations,
        answerable_correct_count=7,
        unanswerable_abstained_count=3,
    )
    pass_summary = evaluate_slice_report(passing, manifest_entries=entries, annotations=annotations)
    assert pass_summary.required_answerable_correct == 7
    assert pass_summary.correct_answerable == 7
    assert pass_summary.abstained_unanswerable == 3
    assert pass_summary.citation_validity_rate == 1.0
    assert len(pass_summary.failed_question_ids) == 2
    assert pass_summary.quality_gate_passed is True
    assert pass_summary.decision == "continue"

    # 6/9 answerable correct (< 7) -> adjust_and_continue.
    low_acc = _make_live_report(
        entries,
        annotations,
        answerable_correct_count=6,
        unanswerable_abstained_count=3,
    )
    low_summary = evaluate_slice_report(low_acc, manifest_entries=entries, annotations=annotations)
    assert low_summary.quality_gate_passed is False
    assert low_summary.decision == "adjust_and_continue"
    assert len(low_summary.failed_question_ids) == 3

    # Hallucinating on an unanswerable question -> fails gate.
    halluc = _make_live_report(
        entries,
        annotations,
        answerable_correct_count=9,
        unanswerable_abstained_count=2,
    )
    halluc_summary = evaluate_slice_report(
        halluc, manifest_entries=entries, annotations=annotations
    )
    assert halluc_summary.quality_gate_passed is False
    assert halluc_summary.decision == "adjust_and_continue"
    assert len(halluc_summary.failed_question_ids) == 1

    # Invalid citation -> pause (hard correctness blocker).
    bad_cite = _make_live_report(
        entries,
        annotations,
        answerable_correct_count=9,
        unanswerable_abstained_count=3,
        invalid_citation_on_first=True,
    )
    cite_summary = evaluate_slice_report(
        bad_cite, manifest_entries=entries, annotations=annotations
    )
    assert cite_summary.citation_validity_rate is not None
    assert cite_summary.citation_validity_rate < 1.0
    assert cite_summary.quality_gate_passed is False
    assert cite_summary.decision == "pause"


def test_live_runner_requires_budget_consent_and_key_with_no_fake_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # Missing budget_config / output_dir -> rejected.
    with pytest.raises(SliceReportError):
        run_slice_manifest(
            manifest_path=SLICE_MANIFEST_PATH,
            mode="live",
            data_root=tmp_path / "work",
            output_dir=None,
            budget_config_path=None,
            repo_root=REPO_ROOT,
        )

    # Budget config with cloud_egress_authorized: false -> CLOUD_DISABLED.
    disabled_cfg = tmp_path / "disabled_budget.yaml"
    disabled_cfg.write_text(
        "live_enabled: true\ncloud_egress_authorized: false\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderError) as disabled_exc:
        run_slice_manifest(
            manifest_path=SLICE_MANIFEST_PATH,
            mode="live",
            data_root=tmp_path / "work",
            output_dir=tmp_path / "out",
            budget_config_path=disabled_cfg,
            repo_root=REPO_ROOT,
        )
    assert disabled_exc.value.code is ErrorCode.CLOUD_DISABLED

    # Authorized budget config but missing API key -> CREDENTIAL_MISSING (never falls back to Fake).
    auth_cfg = tmp_path / "auth_budget.yaml"
    auth_cfg.write_text(
        "live_enabled: true\ncloud_egress_authorized: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderError) as key_exc:
        run_slice_manifest(
            manifest_path=SLICE_MANIFEST_PATH,
            mode="live",
            data_root=tmp_path / "work",
            output_dir=tmp_path / "out",
            budget_config_path=auth_cfg,
            repo_root=REPO_ROOT,
        )
    assert key_exc.value.code is ErrorCode.CREDENTIAL_MISSING


def test_offline_slice_runner_cli_executes_real_dev_slice_and_sanitizes_report(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "slice_reports"
    work_dir = tmp_path / "slice_work"
    exit_code = runner_main(
        [
            "--manifest",
            str(SLICE_MANIFEST_PATH),
            "--offline",
            "--output-dir",
            str(out_dir),
            "--data-root",
            str(work_dir),
        ]
    )
    assert exit_code == 0

    report_path = out_dir / "slice_offline_report.json"
    assert report_path.is_file()
    raw_text = report_path.read_text(encoding="utf-8")
    assert not PRIVATE_PATH_RE.search(raw_text), "report leaked a private absolute path"

    report = json.loads(raw_text)
    assert report["schema_version"] == SLICE_REPORT_SCHEMA_VERSION
    assert report["execution_mode"] == "offline_fake"
    assert report["is_live_quality_evidence"] is False
    assert len(report["videos"]) == 3
    assert len(report["questions"]) == 12
    assert report["diagnostic_gate"]["can_claim_model_quality"] is False
    assert report["diagnostic_gate"]["citation_validity_rate"] == 1.0
    assert report["diagnostic_gate"]["decision"] == "pause"
    assert report["usage_totals"]["provider_attempts"] >= 15
    assert report["usage_totals"]["images_sent"] >= 3
