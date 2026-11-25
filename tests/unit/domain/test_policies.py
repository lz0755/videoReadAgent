"""Unit tests for M1.2 domain errors and single-source policies."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from video_agent.domain.config import AppConfig, PriceConfig
from video_agent.domain.errors import (
    BudgetError,
    ErrorCode,
    ProviderError,
    RetryClass,
    classify_retry,
)
from video_agent.domain.policies import (
    build_budget_limits,
    check_cloud_egress_allowed,
    clamp_request_timeout_seconds,
    estimate_cost_usd,
    should_attempt_schema_repair,
    should_retry_transient,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_error_retry_classification() -> None:
    """Retry classification separates transient, repairable, manual-unknown, and fatal errors."""
    assert classify_retry(ErrorCode.RATE_LIMITED) is RetryClass.TRANSIENT_RETRYABLE
    assert classify_retry(ErrorCode.PROVIDER_TIMEOUT) is RetryClass.TRANSIENT_RETRYABLE
    assert classify_retry(ErrorCode.PROVIDER_TRANSIENT_ERROR) is RetryClass.TRANSIENT_RETRYABLE
    assert classify_retry(ErrorCode.SCHEMA_VALIDATION_FAILED) is RetryClass.REPAIRABLE_ONCE
    assert (
        classify_retry(ErrorCode.EXTERNAL_RESULT_UNKNOWN) is RetryClass.MANUAL_CONFIRMATION_REQUIRED
    )
    assert classify_retry(ErrorCode.CREDENTIAL_MISSING) is RetryClass.NON_RETRYABLE
    assert classify_retry(ErrorCode.CLOUD_DISABLED) is RetryClass.NON_RETRYABLE
    assert classify_retry(ErrorCode.PROVIDER_AUTH_FAILED) is RetryClass.NON_RETRYABLE


def test_budget_limits_built_from_config() -> None:
    """build_budget_limits translates defaults.yaml into canonical analysis & QA limits."""
    raw = yaml.safe_load((REPO_ROOT / "config" / "defaults.yaml").read_text("utf-8"))
    cfg = AppConfig.model_validate(raw)

    analysis_limits = build_budget_limits(cfg, "analysis")
    assert analysis_limits.max_provider_attempts == 90
    assert analysis_limits.max_submitted_image_parts == 720
    assert analysis_limits.max_images_per_request == 8
    assert analysis_limits.deadline_seconds == 3600

    qa_limits = build_budget_limits(cfg, "qa")
    assert qa_limits.max_rounds == 5
    assert qa_limits.max_tool_calls == 6
    assert qa_limits.max_expensive_tool_calls == 2
    assert qa_limits.max_submitted_image_parts == 32
    assert qa_limits.max_images_per_request == 16
    assert qa_limits.max_provider_attempts == 10
    assert qa_limits.deadline_seconds == 180


def test_timeout_clamping_and_expired_deadline() -> None:
    """Network timeout is min(configured, remaining) and raises DEADLINE_EXCEEDED at <= 0."""
    assert clamp_request_timeout_seconds(45.0, 120.0) == 45.0
    assert clamp_request_timeout_seconds(45.0, 12.5) == 12.5
    with pytest.raises(BudgetError) as exc_info:
        clamp_request_timeout_seconds(45.0, 0.0)
    assert exc_info.value.code is ErrorCode.DEADLINE_EXCEEDED


def test_cloud_egress_and_credential_gates() -> None:
    """Cloud egress checks reject disabled upload or missing key explicitly."""
    with pytest.raises(ProviderError) as disabled:
        check_cloud_egress_allowed(cloud_upload_enabled=False, has_credential=True)
    assert disabled.value.code is ErrorCode.CLOUD_DISABLED

    with pytest.raises(ProviderError) as missing:
        check_cloud_egress_allowed(cloud_upload_enabled=True, has_credential=False)
    assert missing.value.code is ErrorCode.CREDENTIAL_MISSING

    check_cloud_egress_allowed(cloud_upload_enabled=True, has_credential=True)


def test_cost_estimation_and_retry_caps() -> None:
    """Cost estimation matches pure per-1M-token formula; retry/repair respect hard caps."""
    raw_prices = yaml.safe_load((REPO_ROOT / "config" / "prices.yaml").read_text("utf-8"))
    prices = PriceConfig.model_validate(raw_prices)

    # 150,000 input tokens at $0.15/1M + 10,000 output tokens at $0.60/1M = 0.0225 + 0.006 = 0.0285
    cost = estimate_cost_usd(
        prices,
        "deepseek-flash",
        input_tokens=150_000,
        output_tokens=10_000,
    )
    assert cost == pytest.approx(0.0285)

    assert should_retry_transient(
        ErrorCode.RATE_LIMITED, transient_retries_used=0, max_transient_retries=2
    )
    assert should_retry_transient(
        ErrorCode.RATE_LIMITED, transient_retries_used=1, max_transient_retries=2
    )
    assert not should_retry_transient(
        ErrorCode.RATE_LIMITED, transient_retries_used=2, max_transient_retries=2
    )
    assert not should_retry_transient(
        ErrorCode.PROVIDER_AUTH_FAILED, transient_retries_used=0, max_transient_retries=2
    )

    assert should_attempt_schema_repair(
        ErrorCode.SCHEMA_VALIDATION_FAILED, repairs_used=0, max_schema_repairs=1
    )
    assert not should_attempt_schema_repair(
        ErrorCode.SCHEMA_VALIDATION_FAILED, repairs_used=1, max_schema_repairs=1
    )
