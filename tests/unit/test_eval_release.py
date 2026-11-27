from datetime import date
from pathlib import Path
from typing import Any

import pytest

from eval.metrics import score
from eval.report import adjudicate, save_json
from eval.runner import _remaining
from tests.unit.test_eval_metrics import references, review, trial
from video_agent.application.budget import SliceRunLimits


def test_a_relabelled_fake_receipt_cannot_enter_adjudication(tmp_path: Path) -> None:
    from eval.manifest import D1_BLIND_SHA

    save_json(
        tmp_path / "report.json",
        {
            "mode": "live",
            "state": "finished",
            "split": "heldout",
            "provenance": {"input_sha256": D1_BLIND_SHA, "models": {"vision": "fake-vision"}},
        },
    )
    with pytest.raises(ValueError, match="formal_blind_run_required"):
        adjudicate(tmp_path, tmp_path / "unused-gold", tmp_path / "unused-reviews")


def test_manifest_budget_is_shared_across_jobs_and_unknown_stops_dispatch() -> None:
    limits = SliceRunLimits(
        max_provider_attempts=5,
        max_submitted_image_parts=12,
        max_estimated_cost_usd=0.5,
        deadline_seconds=60,
    )
    totals = {
        "unknown_cost_records": 0,
        "pending_reservations": 0,
        "committed_attempts": 3,
        "committed_image_parts": 10,
        "committed_cost_usd": 0.4,
    }
    left = _remaining(limits, totals, 4.5)
    assert (left.max_provider_attempts, left.max_submitted_image_parts, left.deadline_seconds) == (
        2,
        2,
        4.5,
    )
    assert left.max_estimated_cost_usd == pytest.approx(0.1)
    for change, seconds, reason in [
        ({"unknown_cost_records": 1}, 4.5, "UNKNOWN"),
        ({"pending_reservations": 1}, 4.5, "UNKNOWN"),
        ({"committed_attempts": 5}, 4.5, "BUDGET"),
        ({}, 0, "DEADLINE"),
    ]:
        with pytest.raises(ValueError, match=reason):
            _remaining(limits, totals | change, seconds)


def test_ambiguous_number_and_unobserved_action_are_scored_as_unsupported() -> None:
    questions, events = references()
    # Two visibly ambiguous propositions in one inferred claim, and another checkable
    # assertion in the prose explaining a gap. Each receives its own adjudication.
    answer = trial("v0", answer_status="partial", claim_count=1, citation_checks=[True])
    grade = review("incorrect", [[False, False]])
    grade.update(other_assertions=[False], error_tags=["20_vs_200", "missing_coverage"])
    result = score(questions, events, {"q0": answer}, {"q0": grade})
    assert result["metrics"]["claim_support"] == {"numerator": 0, "denominator": 3}
    assert result["metrics"]["answer_correct"]["numerator"] == 0
    assert result["error_tags"] == {"20_vs_200": 1, "missing_coverage": 1}


@pytest.mark.parametrize("day,blind,expected", [(27, True, 0), (28, False, 0), (28, True, 1)])
def test_delayed_self_review_is_counted_separately_from_independent_review(
    day: int, blind: bool, expected: int
) -> None:
    questions, events = references()
    grade: dict[str, Any] = review("correct", [[True]])
    grade["second_review"] = {
        "reviewer": "reader-a",
        "reviewed_on": date(2026, 11, day).isoformat(),
        "agreed": True,
        "blind": blind,
    }
    result = score(questions, events, {"q0": trial("v0")}, {"q0": grade})
    assert result["second_reviews"] == {
        "independent": 0,
        "delayed_self": expected,
        "population": 32,
    }
