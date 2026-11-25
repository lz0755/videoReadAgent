"""M0.0 contract tests: explicit authorization, budget gate, dual-channel ledger, and redaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from experiments.probe_gate import (
    REQUIRED_BUDGET_FIELDS,
    ProbeAuthorizationConfig,
    ProbeBudgetConfig,
    ProbeBudgetLedger,
    ProbeGateConfig,
    ProbeGateError,
    sanitize_record,
    sanitize_text,
    validate_files_upload_authorization,
    validate_live_gate,
)

EXAMPLE_BUDGET_PATH = Path("experiments/config/probe_budget.example.yaml")


def _valid_budget_dict() -> dict[str, Any]:
    return {
        "max_model_attempts": 4,
        "max_provider_http_attempts": 3,
        "max_transferred_bytes": 1_000_000,
        "deadline_seconds": 60,
        "per_request_timeout_seconds": 30,
        "max_retries_per_request": 2,
    }


def _authorized_gate_config(
    *,
    cloud_egress: bool = True,
    live_enabled: bool = True,
    files_authorized: bool = False,
    files_ttl: int | None = 3600,
) -> ProbeGateConfig:
    return ProbeGateConfig(
        authorization=ProbeAuthorizationConfig(
            cloud_egress_authorized=cloud_egress,
            live_enabled=live_enabled,
            authorized_by="tester",
            authorization_note="unit test",
            files_api_upload_authorized=files_authorized,
            files_api_ttl_seconds=files_ttl,
        ),
        budget=ProbeBudgetConfig.from_mapping(_valid_budget_dict()),
    )


def test_example_budget_config_defaults_to_cloud_disabled() -> None:
    cfg = ProbeGateConfig.from_yaml(EXAMPLE_BUDGET_PATH)
    assert cfg.authorization.cloud_egress_authorized is False
    assert cfg.authorization.live_enabled is False
    assert cfg.authorization.files_api_upload_authorized is False

    with pytest.raises(ProbeGateError) as exc_info:
        validate_live_gate(
            cfg,
            cli_live_flag=False,
            api_key_env="DEEPSEEK_API_KEY",
            environ={},
        )
    assert exc_info.value.code == "LIVE_FLAG_REQUIRED"


@pytest.mark.parametrize("missing_field", REQUIRED_BUDGET_FIELDS)
def test_missing_required_budget_field_rejects_startup(missing_field: str) -> None:
    raw = _valid_budget_dict()
    del raw[missing_field]
    with pytest.raises(ProbeGateError) as exc_info:
        ProbeBudgetConfig.from_mapping(raw)
    assert exc_info.value.code == "BUDGET_CONFIG_INVALID"
    assert missing_field in exc_info.value.message


@pytest.mark.parametrize("bad_value", [0, -1, True, False, "10", 1.5, None])
def test_invalid_int_budget_values_rejected(bad_value: Any) -> None:
    raw = _valid_budget_dict()
    raw["max_model_attempts"] = bad_value
    with pytest.raises(ProbeGateError) as exc_info:
        ProbeBudgetConfig.from_mapping(raw)
    assert exc_info.value.code == "BUDGET_CONFIG_INVALID"


@pytest.mark.parametrize("bad_deadline", [0, -5, True, float("nan"), float("inf"), "60"])
def test_invalid_deadline_rejected(bad_deadline: Any) -> None:
    raw = _valid_budget_dict()
    raw["deadline_seconds"] = bad_deadline
    with pytest.raises(ProbeGateError) as exc_info:
        ProbeBudgetConfig.from_mapping(raw)
    assert exc_info.value.code == "BUDGET_CONFIG_INVALID"


def test_live_gate_requires_cli_flag_egress_consent_live_enabled_and_credential() -> None:
    # 1. Missing --live flag
    with pytest.raises(ProbeGateError) as e1:
        validate_live_gate(
            _authorized_gate_config(),
            cli_live_flag=False,
            api_key_env="DEEPSEEK_API_KEY",
            environ={"DEEPSEEK_API_KEY": "sk-test-123456"},
        )
    assert e1.value.code == "LIVE_FLAG_REQUIRED"

    # 2. cloud_egress_authorized = False
    with pytest.raises(ProbeGateError) as e2:
        validate_live_gate(
            _authorized_gate_config(cloud_egress=False),
            cli_live_flag=True,
            api_key_env="DEEPSEEK_API_KEY",
            environ={"DEEPSEEK_API_KEY": "sk-test-123456"},
        )
    assert e2.value.code == "CLOUD_DISABLED"

    # 3. live_enabled = False
    with pytest.raises(ProbeGateError) as e3:
        validate_live_gate(
            _authorized_gate_config(live_enabled=False),
            cli_live_flag=True,
            api_key_env="DEEPSEEK_API_KEY",
            environ={"DEEPSEEK_API_KEY": "sk-test-123456"},
        )
    assert e3.value.code == "LIVE_MODE_DISABLED"

    # 4. Missing API key -> explicit CREDENTIAL_MISSING (never silent Fake fallback)
    with pytest.raises(ProbeGateError) as e4:
        validate_live_gate(
            _authorized_gate_config(),
            cli_live_flag=True,
            api_key_env="DEEPSEEK_API_KEY",
            environ={},
        )
    assert e4.value.code == "CREDENTIAL_MISSING"

    # 5. All satisfied -> returns key
    key = validate_live_gate(
        _authorized_gate_config(),
        cli_live_flag=True,
        api_key_env="DEEPSEEK_API_KEY",
        environ={"DEEPSEEK_API_KEY": "sk-test-123456"},
    )
    assert key == "sk-test-123456"


def test_files_api_requires_separate_authorization_and_explicit_ttl() -> None:
    cfg_no_files = _authorized_gate_config(files_authorized=False, files_ttl=3600)
    with pytest.raises(ProbeGateError) as e1:
        validate_files_upload_authorization(cfg_no_files.authorization)
    assert e1.value.code == "FILES_API_UNAUTHORIZED"

    cfg_no_ttl = _authorized_gate_config(files_authorized=True, files_ttl=None)
    with pytest.raises(ProbeGateError) as e2:
        validate_files_upload_authorization(cfg_no_ttl.authorization)
    assert e2.value.code == "FILES_TTL_REQUIRED"

    cfg_short_ttl = _authorized_gate_config(files_authorized=True, files_ttl=60)
    with pytest.raises(ProbeGateError) as e3:
        validate_files_upload_authorization(cfg_short_ttl.authorization)
    assert e3.value.code == "FILES_TTL_REQUIRED"

    cfg_ok = _authorized_gate_config(files_authorized=True, files_ttl=3600)
    assert validate_files_upload_authorization(cfg_ok.authorization) == 3600


def test_ledger_separates_channels_under_shared_bytes_and_deadline() -> None:
    now = [100.0]

    def fake_clock() -> float:
        return now[0]

    budget = ProbeBudgetConfig(
        max_model_attempts=2,
        max_provider_http_attempts=2,
        max_transferred_bytes=500,
        deadline_seconds=30.0,
        per_request_timeout_seconds=20.0,
    )
    ledger = ProbeBudgetLedger(budget=budget, clock=fake_clock)

    a1, t1 = ledger.reserve_provider_http_attempt(operation="files.upload", payload_bytes=200)
    assert t1 == 20.0
    assert ledger.provider_http_attempts == 1
    assert ledger.model_attempts == 0
    assert ledger.transferred_bytes == 200
    ledger.settle(a1, status="confirmed", http_status=200, latency_ms=50)

    # Model attempt shares transferred bytes & deadline
    now[0] = 115.0  # 15s remaining (< 20s per-request timeout)
    a2, t2 = ledger.reserve_model_attempt(
        operation="chat.completions", payload_bytes=200, image_parts=2
    )
    assert t2 == 15.0
    assert ledger.model_attempts == 1
    assert ledger.provider_http_attempts == 1
    assert ledger.transferred_bytes == 400
    assert ledger.sent_image_parts == 2

    # Unknown outcome preserves attempt & bytes and locks cost_status to 'unknown'
    ledger.settle(a2, status="unknown", error_code="TIMEOUT")
    assert ledger.cost_status == "unknown"
    assert ledger.model_attempts == 1
    assert ledger.transferred_bytes == 400

    # Exceeding transferred bytes blocks next call even though attempt slots remain
    with pytest.raises(ProbeGateError) as exc_bytes:
        ledger.reserve_provider_http_attempt(operation="files.upload_2", payload_bytes=150)
    assert exc_bytes.value.code == "TRANSFERRED_BYTES_BUDGET_EXCEEDED"

    # Exceeding shared deadline blocks BOTH channels
    now[0] = 131.0
    with pytest.raises(ProbeGateError) as exc_deadline:
        ledger.reserve_provider_http_attempt(operation="files.delete", payload_bytes=0)
    assert exc_deadline.value.code == "DEADLINE_EXCEEDED"


def test_sanitization_redacts_secrets_base64_and_private_paths() -> None:
    raw = {
        "api_key": "sk-verysecretvalue999",
        "authorization": "Bearer sk-anothersecret123",
        "prompt_tokens": 120,
        "payload": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
        "path": r"C:\Users\alice\private\secret_video.mp4",
        "posix": "/Users/alice/private/frame.png",
    }
    clean = sanitize_record(raw, secrets=("sk-verysecretvalue999",))
    assert "verysecretvalue999" not in str(clean)
    assert "anothersecret123" not in str(clean)
    assert "iVBORw0KGgo" not in str(clean)
    assert "alice" not in str(clean)
    assert clean["prompt_tokens"] == 120
    assert "<REDACTED_IMAGE_DATA_URL" in clean["payload"]
    assert "<REDACTED_LOCAL_PATH>" in sanitize_text(r"C:\Users\bob\video.mp4")
