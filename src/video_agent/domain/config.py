"""Strict Pydantic configuration models for config/defaults.yaml and config/prices.yaml."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, field_validator

from video_agent.domain.models import NonEmptyStr, StrictModel


def _finite_non_negative(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a finite non-negative number")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float < 0.0:
        raise ValueError("value must be a finite non-negative number")
    return as_float


class MediaConfig(StrictModel):
    max_duration_ms: StrictInt = Field(gt=0)
    max_file_bytes: StrictInt = Field(gt=0)


class IngestConfig(StrictModel):
    copy_chunk_bytes: StrictInt = Field(gt=0)


class UiConfig(StrictModel):
    max_upload_bytes: StrictInt = Field(gt=0)


class AsrConfig(StrictModel):
    vad_filter: Literal[False] = False


class AnalysisConfig(StrictModel):
    core_window_ms: StrictInt = Field(gt=0)
    context_overlap_ms: StrictInt = Field(ge=0)
    base_fps: float = Field(gt=0.0)
    max_unique_sampled_frames: StrictInt = Field(gt=0)
    max_submitted_image_parts: StrictInt = Field(gt=0)
    max_images_per_request: StrictInt = Field(gt=0)
    max_provider_attempts: StrictInt = Field(gt=0)
    deadline_seconds: StrictInt = Field(gt=0)


class QaConfig(StrictModel):
    max_rounds: StrictInt = Field(gt=0)
    max_tool_calls: StrictInt = Field(gt=0)
    max_expensive_tool_calls: StrictInt = Field(gt=0)
    max_submitted_image_parts: StrictInt = Field(gt=0)
    max_images_per_request: StrictInt = Field(gt=0)
    max_provider_attempts: StrictInt = Field(gt=0)
    deadline_seconds: StrictInt = Field(gt=0)
    max_clip_ms: StrictInt = Field(gt=0)
    inspect_fps: StrictInt = Field(gt=0)


class ProviderConfig(StrictModel):
    name: NonEmptyStr
    model: NonEmptyStr
    api_key_env: NonEmptyStr
    sdk_max_retries: Literal[0] = 0
    tool_return_mode: Literal["observations_text"] = "observations_text"


class ProviderPolicyConfig(StrictModel):
    request_timeout_seconds: StrictInt = Field(gt=0)
    max_transient_retries: StrictInt = Field(ge=0)
    max_schema_repairs: StrictInt = Field(ge=0)


class PromptsConfig(StrictModel):
    registry_path: NonEmptyStr


class BillingConfig(StrictModel):
    price_config_path: NonEmptyStr


class PrivacyConfig(StrictModel):
    cloud_upload_enabled: StrictBool = False
    log_sensitive_content: StrictBool = False


class AppConfig(StrictModel):
    """Top-level validated P0 configuration."""

    media: MediaConfig
    ingest: IngestConfig
    ui: UiConfig
    asr: AsrConfig
    analysis: AnalysisConfig
    qa: QaConfig
    provider: ProviderConfig
    provider_policy: ProviderPolicyConfig
    prompts: PromptsConfig
    billing: BillingConfig
    privacy: PrivacyConfig


class ModelPrice(StrictModel):
    """Reference per-1M-token rates for a single configured model."""

    input_per_1m_tokens_usd: float = Field(ge=0.0)
    input_cache_hit_per_1m_tokens_usd: float = Field(ge=0.0)
    output_per_1m_tokens_usd: float = Field(ge=0.0)
    estimated_tokens_per_image: StrictInt = Field(ge=0)
    request_overhead_tokens: StrictInt = Field(default=32, ge=0)

    @field_validator(
        "input_per_1m_tokens_usd",
        "input_cache_hit_per_1m_tokens_usd",
        "output_per_1m_tokens_usd",
    )
    @classmethod
    def _validate_rate(cls, value: float) -> float:
        return _finite_non_negative(value)


class PriceConfig(StrictModel):
    """Versioned pricing table with currency, effective date, and source."""

    currency: NonEmptyStr
    effective_date: NonEmptyStr
    source: NonEmptyStr
    models: dict[NonEmptyStr, ModelPrice] = Field(min_length=1)
