"""Submission payloads and the immutable identity shared by CLI and queued work."""

from typing import Literal

from pydantic import Field

from video_agent.application.prompt_registry import canonical_json_sha256
from video_agent.application.sampling import SceneProfile
from video_agent.domain.config import AppConfig
from video_agent.domain.models import StrictModel

ExecutionMode = Literal["offline", "live"]


class AnalysisRequest(StrictModel):
    profile: SceneProfile
    execution_mode: ExecutionMode = "offline"


class QuestionRequest(StrictModel):
    query_text: str = Field(min_length=1, max_length=4096, pattern=r"\S")
    mode: Literal["qa", "event_localization"] = "qa"
    budget_profile: Literal["default"] = "default"


def index_fingerprint(
    config: AppConfig, profile: SceneProfile, mode: ExecutionMode, model: str, high_detail: bool
) -> str:
    return canonical_json_sha256([config.analysis.model_dump(), profile, mode, model, high_detail])
