"""Single-source domain policies for budgets, timeouts, retries, and egress (M1.2)."""

from __future__ import annotations

import math
from typing import Literal

from video_agent.domain.config import AppConfig, PriceConfig
from video_agent.domain.errors import (
    BudgetError,
    ErrorCode,
    ProviderError,
    RetryClass,
    classify_retry,
)
from video_agent.domain.models import BudgetLimits


def build_budget_limits(
    config: AppConfig,
    scope_kind: Literal["analysis", "qa"],
) -> BudgetLimits:
    """Build canonical P0 budget limits from validated AppConfig."""
    if scope_kind == "analysis":
        return BudgetLimits(
            scope_kind="analysis",
            max_unique_sampled_frames=config.analysis.max_unique_sampled_frames,
            max_submitted_image_parts=config.analysis.max_submitted_image_parts,
            max_images_per_request=config.analysis.max_images_per_request,
            max_provider_attempts=config.analysis.max_provider_attempts,
            deadline_seconds=config.analysis.deadline_seconds,
            request_timeout_seconds=config.provider_policy.request_timeout_seconds,
            max_transient_retries=config.provider_policy.max_transient_retries,
            max_schema_repairs=config.provider_policy.max_schema_repairs,
            reserve_final_synthesis_attempt=False,
        )
    return BudgetLimits(
        scope_kind="qa",
        max_rounds=config.qa.max_rounds,
        max_tool_calls=config.qa.max_tool_calls,
        max_expensive_tool_calls=config.qa.max_expensive_tool_calls,
        max_submitted_image_parts=config.qa.max_submitted_image_parts,
        max_images_per_request=config.qa.max_images_per_request,
        max_provider_attempts=config.qa.max_provider_attempts,
        deadline_seconds=config.qa.deadline_seconds,
        request_timeout_seconds=config.provider_policy.request_timeout_seconds,
        max_transient_retries=config.provider_policy.max_transient_retries,
        max_schema_repairs=config.provider_policy.max_schema_repairs,
        reserve_final_synthesis_attempt=True,
    )


def clamp_request_timeout_seconds(
    configured_timeout: float,
    remaining_deadline_seconds: float,
) -> float:
    """Return min(configured_timeout, remaining_deadline_seconds), rejecting expired deadlines."""
    if not math.isfinite(configured_timeout) or configured_timeout <= 0.0:
        raise BudgetError(
            ErrorCode.BUDGET_EXCEEDED,
            "configured request timeout must be positive and finite",
        )
    if not math.isfinite(remaining_deadline_seconds) or remaining_deadline_seconds <= 0.0:
        raise BudgetError(
            ErrorCode.DEADLINE_EXCEEDED,
            "task deadline has expired; no new external call allowed",
        )
    return min(float(configured_timeout), float(remaining_deadline_seconds))


def estimate_cost_usd(
    price_config: PriceConfig,
    model_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    image_count: int = 0,
    cache_hit_tokens: int = 0,
) -> float:
    """Compute reference USD cost from configured per-1M-token rates."""
    if model_id not in price_config.models:
        raise ProviderError(
            ErrorCode.CAPABILITY_MISMATCH,
            f"model '{model_id}' is missing from price configuration",
        )
    for field_name, val in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("image_count", image_count),
        ("cache_hit_tokens", cache_hit_tokens),
    ):
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise BudgetError(
                ErrorCode.BUDGET_EXCEEDED,
                f"{field_name} must be a non-negative integer",
            )
    price = price_config.models[model_id]
    effective_input_tokens = input_tokens + image_count * price.estimated_tokens_per_image
    input_cost = (effective_input_tokens / 1_000_000.0) * price.input_per_1m_tokens_usd
    cache_cost = (cache_hit_tokens / 1_000_000.0) * price.input_cache_hit_per_1m_tokens_usd
    output_cost = (output_tokens / 1_000_000.0) * price.output_per_1m_tokens_usd
    return input_cost + cache_cost + output_cost


def check_cloud_egress_allowed(
    *,
    cloud_upload_enabled: bool,
    has_credential: bool,
) -> None:
    """Enforce explicit cloud egress consent and credential presence."""
    if not cloud_upload_enabled:
        raise ProviderError(
            ErrorCode.CLOUD_DISABLED,
            "cloud egress is disabled; explicit user consent is required",
        )
    if not has_credential:
        raise ProviderError(
            ErrorCode.CREDENTIAL_MISSING,
            "provider API credential is missing; no silent fallback allowed",
        )


def should_retry_transient(
    code: ErrorCode,
    *,
    transient_retries_used: int,
    max_transient_retries: int,
) -> bool:
    """Return True iff the error is transient-retryable and retries remain."""
    if classify_retry(code) is not RetryClass.TRANSIENT_RETRYABLE:
        return False
    return transient_retries_used < max_transient_retries


def should_attempt_schema_repair(
    code: ErrorCode,
    *,
    repairs_used: int,
    max_schema_repairs: int,
) -> bool:
    """Return True iff schema validation failed and repair budget remains."""
    if classify_retry(code) is not RetryClass.REPAIRABLE_ONCE:
        return False
    return repairs_used < max_schema_repairs
