"""GPT-6 Luna wire format for the two M1.5 Chat Completions requests."""

from typing import Any

from pydantic import Field

from video_agent.domain.models import AnswerStatus, StrictModel, SupportKind
from video_agent.domain.policies import SLICE_MAX_OUTPUT_TOKENS

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL_ID = "gpt-6-luna"


class _Observation(StrictModel):
    kind: SupportKind
    text: str = Field(description="Describe the evidence and quote legible text verbatim.")
    evidence_ids: list[str]
    start_ms: int = Field(description="First supporting actual frame PTS or transcript start.")
    end_ms: int = Field(description="Exclusive end of the cited evidence in milliseconds.")


class _Observations(StrictModel):
    observations: list[_Observation]


class _Claim(StrictModel):
    text: str
    support_kind: SupportKind
    evidence_ids: list[str]
    limitations: list[str]


class _Answer(StrictModel):
    status: AnswerStatus
    claims: list[_Claim]
    gaps: list[str]


def openai_completion_options(*, vision: bool) -> dict[str, Any]:
    schema = _Observations if vision else _Answer
    return {
        "max_completion_tokens": SLICE_MAX_OUTPUT_TOKENS,
        "reasoning_effort": "none",
        "service_tier": "default",
        "prompt_cache_options": {"mode": "explicit"},
        "store": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "observations" if vision else "answer",
                "strict": True,
                "schema": schema.model_json_schema(),
            },
        },
    }
