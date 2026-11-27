"""Explicit timeline locations shared by the slice and published-index readers."""

import re
from decimal import Decimal

from video_agent.domain.models import TimeRangeMs

_SECONDS = r"(?:seconds?|secs?|s)(?![a-z])|秒"
_NUMBER = r"[0-9]{1,9}(?:\.[0-9]{1,3})?"
_LOCATION = re.compile(
    rf"(?:(?<![a-z0-9])(?P<cue>at|around|near|between|from)\b\s*"
    rf"(?:(?:approximately|about)\s+)?|(?:在|从|第)\s*(?:大约|约)?)"
    rf"\s*(?P<first>{_NUMBER})\s*"
    rf"(?:(?:(?:{_SECONDS})\s*)?(?:[-–—]|\bto\b|\band\b|至|到|和)\s*"
    rf"(?P<last>{_NUMBER})\s*)?(?:{_SECONDS})"
)
_DURATION_PREFIX = re.compile(r"\b(?:for|lasts?|lasted|takes?|took|wait(?:s|ed|ing)?)\s*$")
_DURATION_SUFFIX = re.compile(r"\s*(?:(?:之|以)?内|long\b|in length\b)")
_NEARBY_MS = 1000


def question_times(query: str) -> tuple[int, int] | None:
    """Recognize explicit second-based locations in the question, including decimal seconds."""
    for match in _LOCATION.finditer(query):
        if _DURATION_SUFFIX.match(query, match.end()):
            continue
        if match["cue"] in {"around", "near", "between"} and _DURATION_PREFIX.search(
            query[: match.start()]
        ):
            continue
        first = int(Decimal(match["first"]) * 1000)
        last = int(Decimal(match["last"] or match["first"]) * 1000)
        return min(first, last), max(first, last)
    return None


def nearby_window(anchors: tuple[int, int], bound: TimeRangeMs | None) -> TimeRangeMs | None:
    start = max(0, anchors[0] - _NEARBY_MS)
    end = anchors[1] + _NEARBY_MS + 1
    if bound is not None:
        start = max(start, bound.start_ms)
        end = min(end, bound.end_ms)
    return TimeRangeMs(start_ms=start, end_ms=end) if start < end else None
