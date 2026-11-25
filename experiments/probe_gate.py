"""M0.0 explicit authorization, probe budget validation, accounting ledger, and redaction.

No network calls happen at import time or during offline validation.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

CostStatus = Literal["estimated", "confirmed", "unknown"]
AttemptChannel = Literal["model", "provider_http"]
AttemptOutcome = Literal["reserved", "confirmed", "failed", "unknown"]

REQUIRED_BUDGET_FIELDS = (
    "max_model_attempts",
    "max_provider_http_attempts",
    "max_transferred_bytes",
    "deadline_seconds",
)

MIN_FILES_TTL_SECONDS = 3600
MAX_FILES_TTL_SECONDS = 2592000
MAX_PER_REQUEST_TIMEOUT_SECONDS = 45.0
MAX_APP_RETRIES = 2


class ProbeGateError(RuntimeError):
    """Structured error raised when authorization, credentials, or budget gates block execution."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _require_strict_positive_int(data: Mapping[str, Any], key: str) -> int:
    if key not in data or data[key] is None:
        raise ProbeGateError("BUDGET_CONFIG_INVALID", f"Missing required budget field: {key}")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbeGateError(
            "BUDGET_CONFIG_INVALID",
            f"Budget field {key!r} must be a strict positive int, got {value!r}",
        )
    return value


def _require_strict_non_negative_int(
    data: Mapping[str, Any],
    key: str,
    *,
    default: int,
    max_value: int,
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > max_value:
        raise ProbeGateError(
            "BUDGET_CONFIG_INVALID",
            f"Budget field {key!r} must be an int in [0, {max_value}], got {value!r}",
        )
    return value


def _require_strict_positive_number(data: Mapping[str, Any], key: str) -> float:
    if key not in data or data[key] is None:
        raise ProbeGateError("BUDGET_CONFIG_INVALID", f"Missing required budget field: {key}")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeGateError(
            "BUDGET_CONFIG_INVALID",
            f"Budget field {key!r} must be a positive number, got {value!r}",
        )
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ProbeGateError(
            "BUDGET_CONFIG_INVALID",
            f"Budget field {key!r} must be finite and > 0, got {value!r}",
        )
    return numeric


@dataclass(frozen=True, slots=True)
class ProbeBudgetConfig:
    """Hard caps for M0 live probes. All four core fields are mandatory."""

    max_model_attempts: int
    max_provider_http_attempts: int
    max_transferred_bytes: int
    deadline_seconds: float
    per_request_timeout_seconds: float = MAX_PER_REQUEST_TIMEOUT_SECONDS
    max_retries_per_request: int = MAX_APP_RETRIES

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProbeBudgetConfig:
        max_model_attempts = _require_strict_positive_int(raw, "max_model_attempts")
        max_provider_http_attempts = _require_strict_positive_int(raw, "max_provider_http_attempts")
        max_transferred_bytes = _require_strict_positive_int(raw, "max_transferred_bytes")
        deadline_seconds = _require_strict_positive_number(raw, "deadline_seconds")

        per_request_raw = raw.get("per_request_timeout_seconds", MAX_PER_REQUEST_TIMEOUT_SECONDS)
        if isinstance(per_request_raw, bool) or not isinstance(per_request_raw, (int, float)):
            raise ProbeGateError(
                "BUDGET_CONFIG_INVALID",
                f"per_request_timeout_seconds must be numeric, got {per_request_raw!r}",
            )
        per_request_timeout = float(per_request_raw)
        if (
            not math.isfinite(per_request_timeout)
            or per_request_timeout <= 0
            or per_request_timeout > MAX_PER_REQUEST_TIMEOUT_SECONDS
        ):
            raise ProbeGateError(
                "BUDGET_CONFIG_INVALID",
                f"per_request_timeout_seconds must be in (0, {MAX_PER_REQUEST_TIMEOUT_SECONDS}]",
            )

        max_retries = _require_strict_non_negative_int(
            raw,
            "max_retries_per_request",
            default=MAX_APP_RETRIES,
            max_value=MAX_APP_RETRIES,
        )
        return cls(
            max_model_attempts=max_model_attempts,
            max_provider_http_attempts=max_provider_http_attempts,
            max_transferred_bytes=max_transferred_bytes,
            deadline_seconds=deadline_seconds,
            per_request_timeout_seconds=per_request_timeout,
            max_retries_per_request=max_retries,
        )


@dataclass(frozen=True, slots=True)
class ProbeAuthorizationConfig:
    """Explicit opt-in authorization for live cloud egress and Files API uploads."""

    cloud_egress_authorized: bool = False
    live_enabled: bool = False
    authorized_by: str = ""
    authorization_note: str = ""
    files_api_upload_authorized: bool = False
    files_api_ttl_seconds: int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProbeAuthorizationConfig:
        def _strict_bool(name: str, default: bool = False) -> bool:
            val = raw.get(name, default)
            if not isinstance(val, bool):
                raise ProbeGateError(
                    "AUTHORIZATION_CONFIG_INVALID",
                    f"Authorization field {name!r} must be bool, got {val!r}",
                )
            return val

        ttl_raw = raw.get("files_api_ttl_seconds")
        ttl: int | None
        if ttl_raw is None:
            ttl = None
        elif isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int):
            raise ProbeGateError(
                "AUTHORIZATION_CONFIG_INVALID",
                f"files_api_ttl_seconds must be int or null, got {ttl_raw!r}",
            )
        else:
            ttl = ttl_raw

        return cls(
            cloud_egress_authorized=_strict_bool("cloud_egress_authorized", False),
            live_enabled=_strict_bool("live_enabled", False),
            authorized_by=str(raw.get("authorized_by", "")).strip(),
            authorization_note=str(raw.get("authorization_note", "")).strip(),
            files_api_upload_authorized=_strict_bool("files_api_upload_authorized", False),
            files_api_ttl_seconds=ttl,
        )


@dataclass(frozen=True, slots=True)
class ProbeGateConfig:
    """Combined M0.0 authorization + budget configuration."""

    authorization: ProbeAuthorizationConfig
    budget: ProbeBudgetConfig

    @classmethod
    def from_yaml(cls, path: Path) -> ProbeGateConfig:
        if not path.exists():
            raise ProbeGateError("BUDGET_CONFIG_MISSING", f"Budget config not found: {path.name}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ProbeGateError("BUDGET_CONFIG_INVALID", "Budget config root must be a mapping")
        auth_raw = loaded.get("authorization")
        budget_raw = loaded.get("budget")
        if not isinstance(auth_raw, Mapping):
            raise ProbeGateError("AUTHORIZATION_CONFIG_INVALID", "Missing 'authorization' mapping")
        if not isinstance(budget_raw, Mapping):
            raise ProbeGateError("BUDGET_CONFIG_INVALID", "Missing 'budget' mapping")
        return cls(
            authorization=ProbeAuthorizationConfig.from_mapping(auth_raw),
            budget=ProbeBudgetConfig.from_mapping(budget_raw),
        )


def validate_live_gate(
    config: ProbeGateConfig,
    *,
    cli_live_flag: bool,
    api_key_env: str,
    environ: Mapping[str, str] | None = None,
    require_files_upload: bool = False,
) -> str:
    """Verify all M0.0 preconditions for live cloud probe execution and return API key.

    Raises ProbeGateError without ever falling back to Fake or another provider.
    """
    if not cli_live_flag:
        raise ProbeGateError(
            "LIVE_FLAG_REQUIRED",
            "Live probe requires explicit --live CLI switch; refusing outbound calls.",
        )
    if not config.authorization.cloud_egress_authorized:
        raise ProbeGateError(
            "CLOUD_DISABLED",
            "cloud_egress_authorized is false; refusing to send data to cloud provider.",
        )
    if not config.authorization.live_enabled:
        raise ProbeGateError(
            "LIVE_MODE_DISABLED",
            "live_enabled is false in probe config; refusing outbound calls.",
        )
    if require_files_upload:
        validate_files_upload_authorization(config.authorization)

    env_map = os.environ if environ is None else environ
    api_key = env_map.get(api_key_env, "").strip()
    if not api_key:
        raise ProbeGateError(
            "CREDENTIAL_MISSING",
            f"Required environment variable {api_key_env!r} is missing or empty.",
        )
    return api_key


def validate_files_upload_authorization(auth: ProbeAuthorizationConfig) -> int:
    """Ensure Files API upload has separate explicit opt-in and a valid short TTL."""
    if not auth.files_api_upload_authorized:
        raise ProbeGateError(
            "FILES_API_UNAUTHORIZED",
            "Files API probe requires separate files_api_upload_authorized=true.",
        )
    ttl = auth.files_api_ttl_seconds
    if ttl is None or ttl < MIN_FILES_TTL_SECONDS or ttl > MAX_FILES_TTL_SECONDS:
        raise ProbeGateError(
            "FILES_TTL_REQUIRED",
            (
                f"Files API probe requires explicit files_api_ttl_seconds in "
                f"[{MIN_FILES_TTL_SECONDS}, {MAX_FILES_TTL_SECONDS}] so files are not kept "
                "permanently."
            ),
        )
    return ttl


@dataclass(slots=True)
class LedgerAttempt:
    """Single reserved/settled attempt record (model inference or provider HTTP)."""

    attempt_id: str
    channel: AttemptChannel
    operation: str
    payload_bytes: int
    image_parts: int
    status: AttemptOutcome = "reserved"
    http_status: int | None = None
    error_code: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class ProbeBudgetLedger:
    """Tracks model attempts, provider HTTP attempts, transferred bytes, and shared deadline.

    - Reserve before send.
    - Retries consume new attempts and transferred bytes.
    - Unknown outcomes remain accounted and set cost_status='unknown' (never refunded as zero).
    """

    budget: ProbeBudgetConfig
    clock: Callable[[], float] = time.monotonic
    _started_monotonic: float = field(init=False)
    model_attempts: int = 0
    provider_http_attempts: int = 0
    transferred_bytes: int = 0
    sent_image_parts: int = 0
    cost_status: CostStatus = "estimated"
    attempts: list[LedgerAttempt] = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self) -> None:
        self._started_monotonic = self.clock()

    def remaining_deadline_seconds(self) -> float:
        elapsed = self.clock() - self._started_monotonic
        if elapsed < 0:
            raise ProbeGateError("CLOCK_REGRESSION", "Monotonic clock moved backwards.")
        return max(0.0, self.budget.deadline_seconds - elapsed)

    def effective_request_timeout_seconds(self) -> float:
        remaining = self.remaining_deadline_seconds()
        if remaining <= 0:
            raise ProbeGateError("DEADLINE_EXCEEDED", "Probe total deadline has expired.")
        return min(self.budget.per_request_timeout_seconds, remaining)

    def _reserve(
        self,
        *,
        channel: AttemptChannel,
        operation: str,
        payload_bytes: int,
        image_parts: int,
    ) -> tuple[LedgerAttempt, float]:
        if (
            isinstance(payload_bytes, bool)
            or not isinstance(payload_bytes, int)
            or payload_bytes < 0
        ):
            raise ProbeGateError(
                "INVALID_PAYLOAD_BYTES", f"Invalid payload_bytes: {payload_bytes!r}"
            )
        if isinstance(image_parts, bool) or not isinstance(image_parts, int) or image_parts < 0:
            raise ProbeGateError("INVALID_IMAGE_PARTS", f"Invalid image_parts: {image_parts!r}")

        timeout_s = self.effective_request_timeout_seconds()

        if channel == "model":
            if self.model_attempts + 1 > self.budget.max_model_attempts:
                raise ProbeGateError(
                    "MODEL_ATTEMPT_BUDGET_EXCEEDED",
                    (
                        f"Model attempts cap reached ({self.model_attempts}/"
                        f"{self.budget.max_model_attempts})."
                    ),
                )
        else:
            if self.provider_http_attempts + 1 > self.budget.max_provider_http_attempts:
                raise ProbeGateError(
                    "PROVIDER_HTTP_BUDGET_EXCEEDED",
                    (
                        f"Provider HTTP attempts cap reached ({self.provider_http_attempts}/"
                        f"{self.budget.max_provider_http_attempts})."
                    ),
                )

        if self.transferred_bytes + payload_bytes > self.budget.max_transferred_bytes:
            raise ProbeGateError(
                "TRANSFERRED_BYTES_BUDGET_EXCEEDED",
                (
                    f"Transferred bytes cap exceeded: current={self.transferred_bytes} + "
                    f"next={payload_bytes} > cap={self.budget.max_transferred_bytes}."
                ),
            )

        self._seq += 1
        attempt = LedgerAttempt(
            attempt_id=f"{channel}-{self._seq:04d}",
            channel=channel,
            operation=operation,
            payload_bytes=payload_bytes,
            image_parts=image_parts,
        )
        if channel == "model":
            self.model_attempts += 1
        else:
            self.provider_http_attempts += 1
        self.transferred_bytes += payload_bytes
        self.sent_image_parts += image_parts
        self.attempts.append(attempt)
        return attempt, timeout_s

    def reserve_model_attempt(
        self,
        *,
        operation: str,
        payload_bytes: int,
        image_parts: int = 0,
    ) -> tuple[LedgerAttempt, float]:
        return self._reserve(
            channel="model",
            operation=operation,
            payload_bytes=payload_bytes,
            image_parts=image_parts,
        )

    def reserve_provider_http_attempt(
        self,
        *,
        operation: str,
        payload_bytes: int = 0,
    ) -> tuple[LedgerAttempt, float]:
        return self._reserve(
            channel="provider_http",
            operation=operation,
            payload_bytes=payload_bytes,
            image_parts=0,
        )

    def settle(
        self,
        attempt: LedgerAttempt,
        *,
        status: Literal["confirmed", "failed", "unknown"],
        http_status: int | None = None,
        error_code: str | None = None,
        latency_ms: int | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        attempt.status = status
        attempt.http_status = http_status
        attempt.error_code = error_code
        attempt.latency_ms = latency_ms
        if usage is not None:
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
            tt = usage.get("total_tokens")
            attempt.prompt_tokens = pt if isinstance(pt, int) and not isinstance(pt, bool) else None
            attempt.completion_tokens = (
                ct if isinstance(ct, int) and not isinstance(ct, bool) else None
            )
            attempt.total_tokens = tt if isinstance(tt, int) and not isinstance(tt, bool) else None

        if status == "unknown":
            # Unknown external result/cost can NEVER be downgraded back to confirmed/zero.
            self.cost_status = "unknown"
        elif (
            status == "confirmed"
            and self.cost_status != "unknown"
            and attempt.channel == "model"
            and attempt.total_tokens is not None
        ):
            self.cost_status = "confirmed"

    def summary(self) -> dict[str, Any]:
        return {
            "model_attempts": self.model_attempts,
            "max_model_attempts": self.budget.max_model_attempts,
            "provider_http_attempts": self.provider_http_attempts,
            "max_provider_http_attempts": self.budget.max_provider_http_attempts,
            "transferred_bytes": self.transferred_bytes,
            "max_transferred_bytes": self.budget.max_transferred_bytes,
            "sent_image_parts": self.sent_image_parts,
            "deadline_seconds": self.budget.deadline_seconds,
            "remaining_deadline_seconds": round(self.remaining_deadline_seconds(), 3),
            "cost_status": self.cost_status,
            "attempts": [
                {
                    "attempt_id": a.attempt_id,
                    "channel": a.channel,
                    "operation": a.operation,
                    "payload_bytes": a.payload_bytes,
                    "image_parts": a.image_parts,
                    "status": a.status,
                    "http_status": a.http_status,
                    "error_code": a.error_code,
                    "latency_ms": a.latency_ms,
                    "prompt_tokens": a.prompt_tokens,
                    "completion_tokens": a.completion_tokens,
                    "total_tokens": a.total_tokens,
                }
                for a in self.attempts
            ],
        }


_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")
_WIN_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\s\"'<>|]+\\)*[^\s\"'<>|]*")
_POSIX_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/|/root/)[^\s\"'<>|]+")


def sanitize_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Redact API keys, raw base64 image payloads, and private absolute filesystem paths."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "<REDACTED_SECRET>")

    def _replace_data_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"<REDACTED_IMAGE_DATA_URL len={len(raw)} sha256={digest}>"

    out = _DATA_URL_RE.sub(_replace_data_url, out)
    out = _BEARER_RE.sub(r"\1<REDACTED_TOKEN>", out)
    out = _SK_KEY_RE.sub("<REDACTED_API_KEY>", out)
    out = _WIN_ABS_PATH_RE.sub("<REDACTED_LOCAL_PATH>", out)
    out = _POSIX_HOME_PATH_RE.sub("<REDACTED_LOCAL_PATH>", out)
    return out


def sanitize_record(obj: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively sanitize nested dicts/lists/strings for safe archival and reporting."""
    if isinstance(obj, str):
        return sanitize_text(obj, secrets=secrets)
    if isinstance(obj, Mapping):
        sanitized: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            lower_key = key_str.lower()
            if any(
                sensitive in lower_key
                for sensitive in ("api_key", "authorization", "x-api-key", "secret", "token")
            ) and lower_key not in {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "max_tokens",
                "max_output_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "internal_max_tokens_per_image",
                "deepseek_flash_per_1m_tokens",
                "reasoning_tokens",
                "cached_tokens",
            }:
                if isinstance(v, str) and not v.endswith("_API_KEY"):
                    sanitized[key_str] = "<REDACTED_SECRET>"
                else:
                    sanitized[key_str] = sanitize_record(v, secrets=secrets)
            else:
                sanitized[key_str] = sanitize_record(v, secrets=secrets)
        return sanitized
    if isinstance(obj, list):
        return [sanitize_record(item, secrets=secrets) for item in obj]
    if isinstance(obj, tuple):
        return [sanitize_record(item, secrets=secrets) for item in obj]
    return obj
