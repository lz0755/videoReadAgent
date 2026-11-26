"""Frozen M3 denominators and adjudication rules for the M6 evaluator."""

from collections.abc import Sequence


def event_hits(reference: tuple[str,int,int], predictions: Sequence[tuple[str,int,int]]) -> tuple[int,int]:
    """Return matched boundary successes and extra predictions for one localization request."""
    raise NotImplementedError


def quality_gates(*, correct: int, recall_at_five: int, appropriate_abstentions: int, localized: int,
                  supported_claims:int, claims:int, valid_citations:int,citations:int) -> dict[str,bool]:
    raise NotImplementedError
