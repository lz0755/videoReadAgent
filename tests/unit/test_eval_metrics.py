from typing import Any

import pytest

from eval.manifest import DatasetManifestError, validate_split_groups
from eval.metrics import score

type Row = dict[str, Any]


def references() -> tuple[list[Row], list[Row]]:
    questions = [
        {
            "question_id": f"q{i}",
            "video_id": f"v{i // 4}",
            "category": "small_text",
            "answerability": "not_answerable" if i % 4 == 3 else "answerable",
            "evidence_refs": [{"modality": "visual", "start_ms": 100, "end_ms": 200}],
        }
        for i in range(32)
    ]
    events = [
        {
            "request_id": f"e{i}",
            "video_id": f"v{i % 8}",
            "label": "open",
            "start_ms": 5000,
            "end_ms": 12000,
        }
        for i in range(20)
    ]
    return questions, events


def trial(video_id: str, **fields: Any) -> Row:
    return {
        "video_id": video_id,
        "execution": "completed",
        "answer_status": "answered",
        "claim_count": 1,
        "citation_checks": [True],
        "first_candidates": [],
        "predictions": [],
        **fields,
    }


def review(verdict: str, claims: list[list[bool]]) -> Row:
    return {
        "verdict": verdict,
        "assertions_by_claim": claims,
        "other_assertions": [],
        "reviewer": "reader-a",
        "reviewed_on": "2026-11-27",
    }


def test_frozen_populations_keep_missing_failed_partial_and_extra_predictions() -> None:
    questions, events = references()
    usable = [{"kind": "frame", "point_ms": 100}]
    wrong_end = [{"kind": "frame", "point_ms": 200}]
    trials = {
        "q0": trial("v0", first_candidates=[wrong_end] * 4 + [usable]),
        "q1": trial("v0", first_candidates=[wrong_end] * 5 + [usable], answer_status="partial"),
        "q2": trial(
            "v0",
            execution="failed",
            failure="BUDGET_EXCEEDED",
            claim_count=0,
            citation_checks=[],
            first_candidates=[usable],
        ),
        "q3": trial("v0", answer_status="not_answerable", citation_checks=[], claim_count=0),
        "e0": trial("v0", predictions=[["open", 3000, 14000], ["wrong", 5000, 12000]]),
        "e1": trial("v1", predictions=[["open", 5000, 14001]]),
        "e2": trial("v2", answer_status="partial", predictions=[["open", 5000, 12000]]),
    }
    reviews = {
        "q0": review("correct", [[True, False]]),
        "q1": review("incorrect", [[False]]),
        "q3": review("appropriate", []),
    }
    result = score(questions, events, trials, reviews)
    counts = result["metrics"]
    assert counts["answer_correct"] == {"numerator": 1, "denominator": 24}
    assert counts["first_retrieval_at_five"] == {"numerator": 1, "denominator": 24}
    assert counts["appropriate_abstention"] == {"numerator": 1, "denominator": 8}
    assert counts["event_boundaries"] == {"numerator": 1, "denominator": 20}
    assert counts["claim_support"] == {"numerator": 1, "denominator": 3}
    assert result["extra_predictions"] >= 1 and not result["quality_passed"]
    assert result["slices"]["category"]["small_text"]["answer_correct"] == counts["answer_correct"]
    assert {row["request_id"] for row in result["failures"]} >= {"q1", "q2", "q4", "e1", "e2"}


def test_review_must_cover_every_claim_and_zero_denominators_cannot_pass() -> None:
    questions, events = references()
    result = score(questions, events, {}, {})
    assert result["metrics"]["claim_support"] == {"numerator": 0, "denominator": 0}
    assert not result["gates"]["citation_integrity"] and not result["quality_passed"]
    with pytest.raises(ValueError, match="claim"):
        score(questions, events, {"q0": trial("v0")}, {"q0": review("correct", [])})


def test_invalid_citations_count_and_missing_second_reviews_block_release() -> None:
    questions, events = references()
    trials = {}
    reviews = {}
    for q in questions:
        abstain = q["answerability"] == "not_answerable"
        trials[q["question_id"]] = trial(
            q["video_id"],
            answer_status="not_answerable" if abstain else "answered",
            claim_count=0 if abstain else 1,
            citation_checks=[] if abstain else [True],
            first_candidates=[[{"kind": "frame", "point_ms": 150}]],
        )
        reviews[q["question_id"]] = review(
            "appropriate" if abstain else "correct", [] if abstain else [[True]]
        )
    trials.update(
        {e["request_id"]: trial(e["video_id"], predictions=[["open", 5000, 12000]]) for e in events}
    )
    result = score(questions, events, trials, reviews)
    assert all(result["gates"].values()) and not result["quality_passed"]
    for i in range(7):
        reviews[f"q{i}"]["second_review"] = {
            "reviewer": "reader-b",
            "reviewed_on": "2026-11-27",
            "agreed": True,
        }
    assert score(questions, events, trials, reviews)["quality_passed"]
    trials["q0"]["citation_checks"] = [False]
    bad = score(questions, events, trials, reviews)
    assert bad["metrics"]["citation_integrity"] == {"numerator": 43, "denominator": 44}
    assert not bad["quality_passed"]


@pytest.mark.parametrize("key", ["source_group_id", "original_id", "sha256", "series", "related"])
def test_split_collision_rejected_independently_of_video_id(key: str) -> None:
    first = dict(
        artifact_id="v1",
        split="m0_dev",
        source_group_id="source1",
        original_id="o1",
        sha256="a" * 64,
        series="series1",
        related="related1",
    )
    second = dict(
        artifact_id="v2",
        split="m6_holdout",
        source_group_id="source2",
        original_id="o2",
        sha256="b" * 64,
        series="series2",
        related="related2",
    )
    validate_split_groups([first, second])
    second[key] = first[key]
    with pytest.raises(DatasetManifestError, match="split|duplicate"):
        validate_split_groups([first, second])
