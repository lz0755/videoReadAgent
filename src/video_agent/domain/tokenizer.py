"""Pure Chinese bigram + English term tokenizer shared across M1.5, M3.5, and M4.1.

Zero infrastructure, SDK, database, or HTTP imports.
"""

from __future__ import annotations

import re
import unicodedata

from video_agent.domain.errors import DomainValidationError, ErrorCode

TOKENIZER_VERSION: str = "zh_bigram_en_v1"
DEFAULT_MAX_QUERY_LENGTH: int = 256
DEFAULT_SUBSTRING_FALLBACK_SCAN_LIMIT: int = 500

_SEGMENT_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf]+|[a-z0-9]+)")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def tokenize_text(text: str) -> list[str]:
    """NFKC/lowercase normalize and emit contiguous CJK bigrams + English/numeric terms."""
    normalized = _normalize_text(text)
    tokens: list[str] = []
    for match in _SEGMENT_RE.finditer(normalized):
        chunk = match.group(1)
        if _CJK_CHAR_RE.match(chunk[0]):
            if len(chunk) >= 2:
                for idx in range(len(chunk) - 1):
                    tokens.append(chunk[idx : idx + 2])
        else:
            tokens.append(chunk)
    return tokens


def tokenize_to_fts_string(text: str) -> str:
    """Join tokens with single spaces for FTS5 or inverted-index storage."""
    return " ".join(tokenize_text(text))


def validate_search_query(
    query: str,
    *,
    max_length: int = DEFAULT_MAX_QUERY_LENGTH,
) -> tuple[str, list[str], bool]:
    """Validate and normalize a search query.

    Returns `(normalized_query, tokens, needs_substring_fallback)`.
    Rejects empty/whitespace, punctuation-only, or over-long queries with
    `DomainValidationError(ErrorCode.SCHEMA_VALIDATION_FAILED)`.
    """
    if not isinstance(query, str):
        raise DomainValidationError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            "search query must be a string",
        )
    normalized = _normalize_text(query).strip()
    if not normalized:
        raise DomainValidationError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            "search query must not be empty or whitespace-only",
        )
    if len(normalized) > max_length:
        raise DomainValidationError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            f"search query length ({len(normalized)}) exceeds max_length ({max_length})",
        )

    tokens = tokenize_text(normalized)
    if tokens:
        return normalized, tokens, False

    # Allow bounded literal substring fallback only if at least one CJK character is present.
    if _CJK_CHAR_RE.search(normalized):
        return normalized, [], True

    raise DomainValidationError(
        ErrorCode.SCHEMA_VALIDATION_FAILED,
        "search query contains no indexable CJK or alphanumeric terms",
    )
