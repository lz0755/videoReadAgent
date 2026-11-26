"""Build versioned search documents from a bound set of committed windows."""

from dataclasses import dataclass

from video_agent.application.observations import WindowResult
from video_agent.domain.tokenizer import tokenize_to_fts_string

DOCUMENT_VERSION = "evidence-doc-v1"


@dataclass(frozen=True)
class SearchDocument:
    source_kind: str
    source_id: str
    original_text: str
    tokens: str
    start_ms: int
    end_ms: int


def documents_for(result: WindowResult) -> list[SearchDocument]:
    rows = []
    for item in result.inputs.transcripts:
        if item.text.strip():
            rows.append(
                SearchDocument(
                    "transcript",
                    item.evidence_id,
                    item.text,
                    tokenize_to_fts_string(item.text),
                    item.time_range.start_ms,
                    item.time_range.end_ms,
                )
            )
    for obs in result.observations:
        rows.append(
            SearchDocument(
                "observation",
                obs.observation_id,
                obs.text,
                tokenize_to_fts_string(obs.text),
                obs.time_range.start_ms,
                obs.time_range.end_ms,
            )
        )
    return rows
