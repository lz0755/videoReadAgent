"""Build versioned search documents from a bound set of committed windows."""

from dataclasses import dataclass

from video_agent.application.observations import WindowResult
from video_agent.domain.tokenizer import TOKENIZER_VERSION

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
    raise NotImplementedError
