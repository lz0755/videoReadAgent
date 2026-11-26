"""Frozen M3 denominators and adjudication rules for the M6 evaluator."""

from collections.abc import Sequence
from fractions import Fraction


def _event(value: tuple[str, int, int]) -> None:
    label, start, end = value
    if (
        not isinstance(label, str)
        or not label.strip()
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
    ):
        raise ValueError("invalid labelled time range")


def event_hits(
    reference: tuple[str, int, int], predictions: Sequence[tuple[str, int, int]]
) -> tuple[int, int]:
    """One request has one reference. Match the same label with greatest positive IoU."""
    _event(reference)
    label, start, end = reference
    matches = []
    for p in predictions:
        _event(p)
        name, left, right = p
        overlap = min(right, end) - max(left, start)
        if name == label and overlap > 0:
            iou = Fraction(overlap, max(right, end) - min(left, start))
            matches.append((iou, -left, -right))
    if not matches:
        return 0, len(predictions)
    _, neg_left, neg_right = max(matches)
    return int(abs(-neg_left - start) <= 2000 and abs(-neg_right - end) <= 2000), len(
        predictions
    ) - 1


def quality_gates(
    *,
    correct: int,
    recall_at_five: int,
    appropriate_abstentions: int,
    localized: int,
    supported_claims: int,
    claims: int,
    valid_citations: int,
    citations: int,
) -> dict[str, bool]:
    counts = (
        (correct, 24),
        (recall_at_five, 24),
        (appropriate_abstentions, 8),
        (localized, 20),
        (supported_claims, claims),
        (valid_citations, citations),
    )
    if any(
        type(n) is not int or type(total) is not int or not 0 <= n <= total for n, total in counts
    ):
        raise ValueError("metric count outside its denominator")
    return {
        "answer_correct": correct >= 20,
        "first_retrieval_at_five": recall_at_five >= 22,
        "appropriate_abstention": appropriate_abstentions >= 7,
        "event_boundaries": localized >= 17,
        "claim_support": claims > 0 and 10 * supported_claims >= 9 * claims,
        "citation_integrity": citations > 0 and valid_citations == citations,
    }
