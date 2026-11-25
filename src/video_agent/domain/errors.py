"""Domain error codes and exception hierarchy (M1.2)."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error codes across domain, application, agent, and adapters."""

    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CLOUD_DISABLED = "CLOUD_DISABLED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    NO_FRAME_IN_RANGE = "NO_FRAME_IN_RANGE"
    TIMESTAMP_UNRELIABLE = "TIMESTAMP_UNRELIABLE"
    NO_AUDIO_STREAM = "NO_AUDIO_STREAM"
    CORRUPT_MEDIA = "CORRUPT_MEDIA"
    MEDIA_LIMIT_EXCEEDED = "MEDIA_LIMIT_EXCEEDED"
    FIXTURE_INVALID = "FIXTURE_INVALID"
    SUPPORTED_CLAIM_REQUIRED = "SUPPORTED_CLAIM_REQUIRED"
    GAP_REQUIRED = "GAP_REQUIRED"
    INVALID_CITATION = "INVALID_CITATION"
    CROSS_ASSET_REFERENCE = "CROSS_ASSET_REFERENCE"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    RUN_ID_FORBIDDEN = "RUN_ID_FORBIDDEN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    JOB_CANCELLED = "JOB_CANCELLED"
    EXTERNAL_RESULT_UNKNOWN = "EXTERNAL_RESULT_UNKNOWN"
    CLOCK_SKEW_DETECTED = "CLOCK_SKEW_DETECTED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_TRANSIENT_ERROR = "PROVIDER_TRANSIENT_ERROR"
    INDEX_NOT_READY = "INDEX_NOT_READY"
    WORKER_ALREADY_RUNNING = "WORKER_ALREADY_RUNNING"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STORAGE_ERROR = "STORAGE_ERROR"
    MIGRATION_ERROR = "MIGRATION_ERROR"
    PROMPT_INTEGRITY_ERROR = "PROMPT_INTEGRITY_ERROR"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"


class RetryClass(StrEnum):
    """Explicit retryability classification for domain errors."""

    NON_RETRYABLE = "non_retryable"
    TRANSIENT_RETRYABLE = "transient_retryable"
    REPAIRABLE_ONCE = "repairable_once"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"


_TRANSIENT_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_TIMEOUT,
        ErrorCode.PROVIDER_TRANSIENT_ERROR,
    }
)


def classify_retry(code: ErrorCode) -> RetryClass:
    """Return the retry classification for a stable error code."""
    if code in _TRANSIENT_CODES:
        return RetryClass.TRANSIENT_RETRYABLE
    if code is ErrorCode.SCHEMA_VALIDATION_FAILED:
        return RetryClass.REPAIRABLE_ONCE
    if code is ErrorCode.EXTERNAL_RESULT_UNKNOWN:
        return RetryClass.MANUAL_CONFIRMATION_REQUIRED
    return RetryClass.NON_RETRYABLE


class VideoAgentError(Exception):
    """Base error for all domain and application failures."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_class: RetryClass | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message
        self.retry_class = retry_class if retry_class is not None else classify_retry(code)
        self.details: dict[str, object] = dict(details) if details is not None else {}


class DomainValidationError(VideoAgentError):
    """Raised when domain invariants fail."""


class BudgetError(VideoAgentError):
    """Raised when budget, deadline, cancellation, or clock-skew constraints trigger."""


class ProviderError(VideoAgentError):
    """Raised on normalized LLM/Vision provider failures."""


class StorageError(VideoAgentError):
    """Raised on SQLite, migration, or artifact storage failures."""


class MediaError(VideoAgentError):
    """Raised on ffprobe, PyAV, FFmpeg, or ASR media failures."""


class PromptIntegrityError(VideoAgentError):
    """Raised when prompt files fail encoding or SHA-256 verification."""


class WorkerLockError(VideoAgentError):
    """Raised when the single-worker OS lock cannot be acquired."""
