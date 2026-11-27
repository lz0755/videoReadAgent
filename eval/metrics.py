"""Score blind M6 outputs against the frozen D1 references."""

from collections import Counter
from datetime import date
from typing import Any

from eval.d1_metrics import event_hits, quality_gates

type Row = dict[str, Any]

_NAMES = (
    "answer_correct",
    "first_retrieval_at_five",
    "appropriate_abstention",
    "event_boundaries",
    "claim_support",
    "citation_integrity",
)


def _counts() -> dict[str, dict[str, int]]:
    return {key: {"numerator": 0, "denominator": 0} for key in _NAMES}


def _recall(candidates: list[list[Row]], refs: list[Row]) -> bool:
    # Each ranked candidate can cite multiple original frames or transcript spans.
    for group in candidates[:5]:
        for evidence in group:
            for ref in refs:
                start, end, modality = ref["start_ms"], ref["end_ms"], ref["modality"]
                if (
                    evidence["kind"] == "frame"
                    and modality in {"visual", "visual_and_audio", "subtitle_and_audio"}
                    and start <= evidence["point_ms"] < end
                ):
                    return True
                if (
                    evidence["kind"] == "transcript"
                    and modality in {"audio", "visual_and_audio", "subtitle_and_audio"}
                    and max(start, evidence["start_ms"]) < min(end, evidence["end_ms"])
                ):
                    return True
    return False


def _review_counts(review: Row, claim_count: int) -> tuple[int, int, str | None]:
    claims, other = review["assertions_by_claim"], review["other_assertions"]
    if (
        not isinstance(claims, list)
        or len(claims) != claim_count
        or not all(isinstance(verdicts, list) and verdicts for verdicts in claims)
    ):
        raise ValueError("each output claim requires a factual support review")
    marks = [mark for verdicts in claims for mark in verdicts] + other
    if any(type(mark) is not bool for mark in marks):
        raise ValueError("claim support requires boolean adjudications")
    reviewer = review["reviewer"]
    day = date.fromisoformat(review["reviewed_on"])
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("missing primary reviewer")
    second = review.get("second_review")
    method = None
    if second is not None:
        who = second["reviewer"]
        when = date.fromisoformat(second["reviewed_on"])
        if not isinstance(who, str) or not who.strip() or when < day:
            raise ValueError("invalid second reviewer")
        if second["agreed"] is True:
            if who.strip().casefold() != reviewer.strip().casefold():
                method = "independent"
            elif when > day and second.get("blind") is True:
                method = "delayed_self"
    return sum(marks), len(marks), method


def score(
    questions: list[Row], events: list[Row], trials: dict[str, Row], reviews: dict[str, Row]
) -> Row:
    """Count every frozen request once. Only an evaluator supplies semantic judgements."""
    regular = {q["question_id"]: q for q in questions}
    requests = {e["request_id"]: e for e in events}
    if (
        len(questions) != len(regular)
        or len(events) != len(requests)
        or len(requests) != 20
        or len({q["video_id"] for q in questions}) != 8
        or Counter(q["video_id"] for q in questions)
        != dict.fromkeys({q["video_id"] for q in questions}, 4)
        or Counter(q["answerability"] for q in questions) != {"answerable": 24, "not_answerable": 8}
        or {e["video_id"] for e in events} != {q["video_id"] for q in questions}
        or regular.keys() & requests.keys()
        or trials.keys() - (regular | requests).keys()
        or reviews.keys() - regular.keys()
    ):
        raise ValueError("invalid frozen evaluation population or unknown request")
    metrics = _counts()
    slices: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    statuses: dict[str, Counter[str]] = {"qa": Counter(), "event_localization": Counter()}
    failures = []
    pending = []
    extra = 0
    rereviews: Counter[str] = Counter()
    error_tags: Counter[str] = Counter()
    for rid, ref in (regular | requests).items():
        event = rid in requests
        trial = trials.get(rid, {})
        if trial and trial["video_id"] != ref["video_id"]:
            raise ValueError("trial belongs to different video")
        completed = trial.get("execution") == "completed"
        status = trial.get("answer_status") if completed else trial.get("execution", "not_run")
        if not isinstance(status, str) or status not in {
            "answered",
            "partial",
            "not_answerable",
            "failed",
            "not_run",
            "running",
        }:
            raise ValueError("invalid trial status")
        statuses["event_localization" if event else "qa"][status] += 1
        eligible = completed and not trial.get("failure")
        checks = trial.get("citation_checks", [])
        if not isinstance(checks, list) or any(type(c) is not bool for c in checks):
            raise ValueError("invalid citation checks")
        values = {"citation_integrity": (sum(checks), len(checks))}
        reasons = []
        if trial.get("failure") or not completed:
            reasons.append(trial.get("failure") or status)
        if not all(checks):
            reasons.append("invalid_citation")
        if event:
            predictions = trial.get("predictions", [])
            hit, surplus = event_hits(
                (ref["label"], ref["start_ms"], ref["end_ms"]),
                [(p[0], p[1], p[2]) for p in predictions],
            )
            valid = eligible and status == "answered" and bool(checks) and all(checks)
            values["event_boundaries"] = (hit if valid else 0, 1)
            extra += surplus if valid else len(predictions)
            if not valid or not hit:
                reasons.append("boundaries_not_matched")
        else:
            answerable = ref["answerability"] == "answerable"
            review = reviews.get(rid)
            verdict = None
            if review is not None:
                if not completed:
                    raise ValueError("review requires a completed answer")
                verdict = review["verdict"]
                if verdict not in (
                    {"correct", "incorrect"} if answerable else {"appropriate", "inappropriate"}
                ):
                    raise ValueError("review verdict conflicts with frozen answerability")
                supported, total, method = _review_counts(review, trial["claim_count"])
                values["claim_support"] = (supported, total)
                if method:
                    rereviews[method] += 1
                if supported < total:
                    reasons.append("unsupported_assertion")
                error_tags.update(review.get("error_tags", []))
            elif completed:
                pending.append(rid)
                reasons.append("review_pending")
            if answerable:
                correct = (
                    eligible
                    and status == "answered"
                    and bool(checks)
                    and all(checks)
                    and verdict == "correct"
                )
                recalled = eligible and _recall(
                    trial.get("first_candidates", []), ref["evidence_refs"]
                )
                values["answer_correct"] = (int(correct), 1)
                values["first_retrieval_at_five"] = (int(recalled), 1)
                if not correct:
                    reasons.append("key_fact_not_verified")
                if not recalled:
                    reasons.append("first_five_miss")
            else:
                appropriate = eligible and status == "not_answerable" and verdict == "appropriate"
                values["appropriate_abstention"] = (int(appropriate), 1)
                if not appropriate:
                    reasons.append("abstention_not_verified")
        buckets = [metrics]
        for dimension in ("video_id", "benchmark", "category", "scene"):
            if dimension in ref:
                buckets.append(
                    slices.setdefault(dimension, {}).setdefault(ref[dimension], _counts())
                )
        for bucket in buckets:
            for key, (num, den) in values.items():
                bucket[key]["numerator"] += num
                bucket[key]["denominator"] += den
        if reasons:
            failures.append({"request_id": rid, "video_id": ref["video_id"], "reasons": reasons})
    gates = quality_gates(
        correct=metrics["answer_correct"]["numerator"],
        recall_at_five=metrics["first_retrieval_at_five"]["numerator"],
        appropriate_abstentions=metrics["appropriate_abstention"]["numerator"],
        localized=metrics["event_boundaries"]["numerator"],
        supported_claims=metrics["claim_support"]["numerator"],
        claims=metrics["claim_support"]["denominator"],
        valid_citations=metrics["citation_integrity"]["numerator"],
        citations=metrics["citation_integrity"]["denominator"],
    )
    return {
        "metrics": metrics,
        "gates": gates,
        "slices": slices,
        "statuses": statuses,
        "extra_predictions": extra,
        "failures": failures,
        "pending_reviews": pending,
        "second_reviews": {
            "independent": rereviews["independent"],
            "delayed_self": rereviews["delayed_self"],
            "population": len(questions),
        },
        "error_tags": dict(error_tags),
        "quality_passed": all(gates.values())
        and not pending
        and rereviews.total() * 5 >= len(questions)
        and all(r.get("second_review", {}).get("agreed", True) is True for r in reviews.values()),
    }
