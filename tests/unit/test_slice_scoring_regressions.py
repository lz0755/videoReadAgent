"""Acceptance cases for inconsistent manual grades and missing citation evidence."""

from typing import Any

import pytest

from eval.manifest import load_dev_seed_annotations
from eval.slice_runner import evaluate_slice_report, load_slice_manifest
from tests.unit.test_slice_report import (
    DEV_ANNOTATIONS_PATH,
    SLICE_MANIFEST_PATH,
    _make_live_report,
)


def test_zero_citations_and_only_abstentions_cannot_pass() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)
    report = _make_live_report(entries, annotations)
    for row in report["questions"]:
        row.update(
            answer_status="not_answerable",
            claims_count=0,
            gaps_count=1,
            total_citations=0,
            valid_citations=0,
        )
    gate = evaluate_slice_report(report, manifest_entries=entries, annotations=annotations)
    assert gate.correct_answerable == 0
    assert gate.abstained_unanswerable == 3
    assert gate.citation_validity_rate is None
    assert gate.quality_gate_passed is False
    assert gate.decision == "pause"


@pytest.mark.parametrize(
    ("answerability", "overrides"),
    [
        (
            "answerable",
            {
                "answer_status": "not_answerable",
                "claims_count": 0,
                "gaps_count": 1,
                "total_citations": 0,
                "valid_citations": 0,
            },
        ),
        ("answerable", {"total_citations": 0, "valid_citations": 0}),
        ("not_answerable", {"claims_count": 1, "total_citations": 1, "valid_citations": 1}),
        ("not_answerable", {"gaps_count": 0}),
    ],
)
def test_inconsistent_row_cannot_supply_the_seventh_correct_answer_or_third_abstention(
    answerability: str,
    overrides: dict[str, Any],
) -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)
    report = _make_live_report(entries, annotations)
    row = next(q for q in report["questions"] if q["answerability"] == answerability)
    row.update(overrides)
    gate = evaluate_slice_report(report, manifest_entries=entries, annotations=annotations)
    assert row["question_id"] in gate.failed_question_ids
    assert gate.quality_gate_passed is False


def test_failed_execution_cannot_be_approved_by_manual_grade() -> None:
    entries = load_slice_manifest(SLICE_MANIFEST_PATH)
    annotations = load_dev_seed_annotations(DEV_ANNOTATIONS_PATH)
    report = _make_live_report(entries, annotations, answerable_correct_count=9)
    row = report["questions"][0]
    row.update(
        execution_status="failed",
        answer_status=None,
        error_code="INVALID_CITATION",
        claims_count=0,
        gaps_count=0,
        total_citations=0,
        valid_citations=0,
    )
    gate = evaluate_slice_report(report, manifest_entries=entries, annotations=annotations)
    assert gate.correct_answerable == 8
    assert row["question_id"] in gate.failed_question_ids
    assert not gate.can_claim_model_quality
    assert gate.decision == "pause"
