"""Application-layer BudgetGuard enforcing single-source P0 policies (M1.7).

Depends exclusively on domain models, errors, policies, and injected ports
(BudgetStore and Clock). Tool layers deduct tool/expensive-tool counts only;
provider send boundaries alone reserve and settle attempts, images, and costs.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import AliasChoices, Field

from video_agent.domain.config import AppConfig, PriceConfig
from video_agent.domain.errors import BudgetError, ErrorCode
from video_agent.domain.models import (
    BudgetAccount,
    BudgetCounters,
    BudgetLimits,
    BudgetReservation,
    CostStatus,
    RequestBudget,
    StrictModel,
    UsageLedger,
    UsageRecord,
)
from video_agent.domain.policies import (
    SLICE_MAX_OUTPUT_TOKENS,
    build_budget_limits,
    clamp_request_timeout_seconds,
    estimate_cost_usd,
)
from video_agent.domain.ports import (
    BudgetStore,
    Clock,
    PlannerProvider,
    PlannerRequest,
    PlannerResponse,
    VisionProvider,
    VisionRequest,
    VisionResponse,
)

_MAX_BACKWARD_SKEW_SECONDS = 5.0


class SliceRunLimits(StrictModel):
    """Numeric approval for a whole manifest, shared by every analysis and QA scope."""

    max_provider_attempts: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("max_provider_attempts", "max_model_attempts"),
    )
    max_submitted_image_parts: int | None = Field(default=None, ge=0)
    max_images_per_request: int | None = Field(default=None, gt=0)
    deadline_seconds: float | None = Field(default=None, gt=0)
    request_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("request_timeout_seconds", "per_request_timeout_seconds"),
    )
    max_estimated_cost_usd: float | None = Field(
        default=None, ge=0, validation_alias=AliasChoices("max_estimated_cost_usd", "max_cost_usd")
    )


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(raw: str) -> datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    return datetime.fromisoformat(normalized).astimezone(UTC)


@dataclass(slots=True)
class _ActiveAttempt:
    reservation: BudgetReservation
    pending: UsageRecord
    dispatched: bool = False

    @property
    def chargeable_images(self) -> int:
        return self.pending.images_sent if self.dispatched else self.reservation.reserved_images


@runtime_checkable
class TransactionalBudgetStore(BudgetStore, Protocol):
    """Optional transactional behavior for jobs persisted in the application catalog."""

    def transaction(self) -> AbstractContextManager[None]: ...
    def check_binding(
        self, scope_id: str, job_id: str, asset_id: str, analysis_id: str
    ) -> None: ...
    def insert_attempt(
        self,
        reservation: BudgetReservation,
        pending: UsageRecord,
        *,
        operation_key: str | None,
        explicit_retry: bool,
        previous_attempt_id: str | None,
        price_version: str,
    ) -> None: ...
    def read_attempt(
        self, attempt_id: str
    ) -> tuple[BudgetReservation, UsageRecord, bool] | None: ...


class BudgetGuard:
    """Atomic pre-send reservation, tool counting, deadline, cancellation, and settlement guard."""

    def __init__(
        self,
        *,
        store: BudgetStore,
        clock: Clock,
        config: AppConfig,
        prices: PriceConfig,
        run_limits: SliceRunLimits | None = None,
    ) -> None:
        self._store = store
        self._durable = store if isinstance(store, TransactionalBudgetStore) else None
        self._clock = clock
        self._config = config
        self._prices = prices
        self.run_limits = run_limits
        duration = run_limits.deadline_seconds if run_limits else None
        self._run_deadline_mono = clock.monotonic() + duration if duration is not None else None
        self._run_deadline_utc = (
            clock.now_utc() + timedelta(seconds=duration) if duration is not None else None
        )
        self._lock = threading.Lock()
        self._known_scopes: dict[str, tuple[Literal["analysis", "qa"], BudgetLimits]] = {}
        self._mono_anchors: dict[str, tuple[float, float]] = {}
        self._last_seen_utc: dict[str, datetime] = {}
        self._active_reservations: dict[str, _ActiveAttempt] = {}

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        with self._lock, self._durable.transaction() if self._durable else nullcontext():
            yield

    def ensure_started(
        self,
        scope_id: str,
        scope_kind: Literal["analysis", "qa"],
        *,
        limits: BudgetLimits | None = None,
    ) -> BudgetAccount:
        """Initialize scope once with persistent first-start deadline; never resets on restart."""
        resolved_limits = (
            limits if limits is not None else build_budget_limits(self._config, scope_kind)
        )
        with self._atomic():
            account = self._store.get_or_create_account(scope_id, scope_kind, resolved_limits)
            now_utc = self._clock.now_utc().astimezone(UTC)
            now_mono = self._clock.monotonic()

            if account.started_at_utc is None or account.deadline_at_utc is None:
                deadline_utc = now_utc + timedelta(seconds=account.limits.deadline_seconds)
                account = account.model_copy(
                    update={
                        "started_at_utc": _format_utc(now_utc),
                        "deadline_at_utc": _format_utc(deadline_utc),
                    }
                )
                self._store.save_account(account)
                remaining_sec = float(account.limits.deadline_seconds)
            else:
                deadline_utc = _parse_utc(account.deadline_at_utc)
                remaining_sec = (deadline_utc - now_utc).total_seconds()

            self._known_scopes[scope_id] = (account.scope_kind, account.limits)
            self._mono_anchors.setdefault(scope_id, (now_mono, remaining_sec))
            self._last_seen_utc.setdefault(scope_id, now_utc)
            return account

    def _load_account_locked(self, scope_id: str) -> BudgetAccount:
        meta = self._known_scopes.get(scope_id)
        if meta is None:
            raise BudgetError(
                ErrorCode.BUDGET_EXCEEDED,
                f"budget scope '{scope_id}' has not been started",
            )
        scope_kind, limits = meta
        return self._store.get_or_create_account(scope_id, scope_kind, limits)

    def _remaining_deadline_seconds_locked(self, account: BudgetAccount) -> float:
        now_utc = self._clock.now_utc().astimezone(UTC)
        now_mono = self._clock.monotonic()

        last_utc = self._last_seen_utc.get(account.scope_id)
        if last_utc is not None:
            backward_delta = (last_utc - now_utc).total_seconds()
            if backward_delta > _MAX_BACKWARD_SKEW_SECONDS:
                raise BudgetError(
                    ErrorCode.CLOCK_SKEW_DETECTED,
                    f"wall clock jumped backwards by {backward_delta:.2f}s; halting execution",
                )
        self._last_seen_utc[account.scope_id] = now_utc

        if account.deadline_at_utc is None:
            raise BudgetError(
                ErrorCode.DEADLINE_EXCEEDED,
                f"budget scope '{account.scope_id}' has no persisted deadline",
            )
        deadline_utc = _parse_utc(account.deadline_at_utc)
        wall_remaining = (deadline_utc - now_utc).total_seconds()

        anchor = self._mono_anchors.get(account.scope_id)
        if anchor is not None:
            anchor_mono, anchor_remaining = anchor
            mono_remaining = anchor_remaining - (now_mono - anchor_mono)
            return min(wall_remaining, mono_remaining)
        return wall_remaining

    def _check_active_locked(self, account: BudgetAccount) -> float:
        if account.cancelled:
            raise BudgetError(
                ErrorCode.JOB_CANCELLED,
                f"scope '{account.scope_id}' is cancelled; no new external call allowed",
            )
        remaining = self._remaining_deadline_seconds_locked(account)
        if self._run_deadline_mono is not None and self._run_deadline_utc is not None:
            remaining = min(
                remaining,
                self._run_deadline_mono - self._clock.monotonic(),
                (self._run_deadline_utc - self._clock.now_utc()).total_seconds(),
            )
        if remaining <= 0.0:
            raise BudgetError(
                ErrorCode.DEADLINE_EXCEEDED,
                f"scope '{account.scope_id}' deadline has expired",
            )
        return remaining

    def remaining_seconds(self, scope_id: str) -> float:
        """Read the active scope/run time left without reserving a provider attempt."""
        with self._atomic():
            return self._check_active_locked(self._load_account_locked(scope_id))

    def cancel(self, scope_id: str) -> BudgetAccount:
        """Mark scope cancelled so no further external calls or tool executions may start."""
        with self._atomic():
            account = self._load_account_locked(scope_id)
            updated = account.model_copy(update={"cancelled": True})
            self._store.save_account(updated)
            return updated

    def begin_round(self, scope_id: str) -> int:
        """Check deadline/cancellation and consume one planner round."""
        with self._atomic():
            account = self._load_account_locked(scope_id)
            self._check_active_locked(account)
            max_rounds = account.limits.max_rounds
            next_round = account.consumed.rounds + 1
            if max_rounds is not None and next_round > max_rounds:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"max_rounds ({max_rounds}) exceeded for scope '{scope_id}'",
                )
            updated_consumed = account.consumed.model_copy(update={"rounds": next_round})
            updated = account.model_copy(update={"consumed": updated_consumed})
            self._store.save_account(updated)
            return next_round

    def record_tool_execution(
        self,
        scope_id: str,
        *,
        tool_name: str,
        expensive: bool = False,
    ) -> str:
        """Atomically deduct tool / expensive tool counters and return a tool_call_id."""
        with self._atomic():
            account = self._load_account_locked(scope_id)
            self._check_active_locked(account)

            next_tools = account.consumed.tool_calls + 1
            if account.limits.max_tool_calls is not None and (
                next_tools > account.limits.max_tool_calls
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"max_tool_calls ({account.limits.max_tool_calls}) exceeded on '{tool_name}'",
                )

            next_expensive = account.consumed.expensive_tool_calls + (1 if expensive else 0)
            if (
                expensive
                and account.limits.max_expensive_tool_calls is not None
                and (next_expensive > account.limits.max_expensive_tool_calls)
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    (
                        f"max_expensive_tool_calls ({account.limits.max_expensive_tool_calls}) "
                        f"exceeded on '{tool_name}'"
                    ),
                )

            updated_consumed = account.consumed.model_copy(
                update={
                    "tool_calls": next_tools,
                    "expensive_tool_calls": next_expensive,
                }
            )
            self._store.save_account(account.model_copy(update={"consumed": updated_consumed}))
            return f"tc_{uuid.uuid4().hex[:12]}"

    def estimate_slice_request(self, model_id: str, prompt_text: str, image_count: int) -> float:
        """Reserve at reference prices using UTF-8 bytes, image allowance and capped output."""
        price = self._prices.models.get(model_id)
        overhead = price.request_overhead_tokens if price is not None else 0
        return estimate_cost_usd(
            self._prices,
            model_id,
            input_tokens=len(prompt_text.encode("utf-8")) + overhead,
            output_tokens=SLICE_MAX_OUTPUT_TOKENS,
            image_count=image_count,
        )

    def _check_run_reservation_locked(self, request: RequestBudget) -> None:
        limits = self.run_limits
        if limits is None:
            return
        if (
            limits.max_images_per_request is not None
            and request.image_count > limits.max_images_per_request
        ):
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "approved images per request exceeded")
        # Consult the same authoritative accounts used for settlement. Pending and unknown
        # sends count even when another question/video asks for the last available slot.
        accounts = [self._load_account_locked(scope) for scope in self._known_scopes]
        counters = [counter for a in accounts for counter in (a.consumed, a.reserved)]
        if limits.max_provider_attempts is not None and (
            1 + sum(c.provider_attempts for c in counters) > limits.max_provider_attempts
        ):
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "approved run attempts exhausted")
        if limits.max_submitted_image_parts is not None and (
            request.image_count + sum(c.submitted_image_parts for c in counters)
            > limits.max_submitted_image_parts
        ):
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "approved run image allowance exhausted")
        if limits.max_estimated_cost_usd is not None and (
            request.estimated_cost_usd + sum(c.estimated_cost_usd for c in counters)
            > limits.max_estimated_cost_usd
        ):
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "approved estimated run cost exceeded")

    def reserve_provider_attempt(
        self,
        request: RequestBudget,
        *,
        operation_key: str | None = None,
        explicit_retry: bool = False,
        previous_attempt_id: str | None = None,
    ) -> BudgetReservation:
        """Atomically reserve one model attempt, image parts, and estimated cost before send."""
        with self._atomic():
            account = self._load_account_locked(request.scope_id)
            remaining_sec = self._check_active_locked(account)
            limits = account.limits

            if request.image_count > limits.max_images_per_request:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    (
                        f"image_count ({request.image_count}) exceeds "
                        f"max_images_per_request ({limits.max_images_per_request})"
                    ),
                )

            total_attempts = account.consumed.provider_attempts + account.reserved.provider_attempts
            effective_attempt_cap = limits.max_provider_attempts
            if limits.reserve_final_synthesis_attempt and not request.is_final_synthesis:
                effective_attempt_cap = max(0, limits.max_provider_attempts - 1)
            if total_attempts + 1 > effective_attempt_cap:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"provider attempt limit ({effective_attempt_cap}) reached",
                )

            total_images = (
                account.consumed.submitted_image_parts + account.reserved.submitted_image_parts
            )
            if total_images + request.image_count > limits.max_submitted_image_parts:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"max_submitted_image_parts ({limits.max_submitted_image_parts}) exceeded",
                )

            self._check_run_reservation_locked(request)
            timeout_sec = clamp_request_timeout_seconds(
                float(limits.request_timeout_seconds),
                remaining_sec,
            )
            if self.run_limits is not None and self.run_limits.request_timeout_seconds is not None:
                timeout_sec = min(timeout_sec, self.run_limits.request_timeout_seconds)

            attempt_id = f"att_{uuid.uuid4().hex[:12]}"
            reservation = BudgetReservation(
                attempt_id=attempt_id,
                scope_id=request.scope_id,
                job_id=request.job_id,
                provider=request.provider,
                model_id=request.model_id,
                parent_tool_call_id=request.parent_tool_call_id,
                reserved_images=request.image_count,
                reserved_cost_usd=request.estimated_cost_usd,
                timeout_seconds=timeout_sec,
                is_final_synthesis=request.is_final_synthesis,
            )

            updated_reserved = account.reserved.model_copy(
                update={
                    "provider_attempts": account.reserved.provider_attempts + 1,
                    "submitted_image_parts": (
                        account.reserved.submitted_image_parts + request.image_count
                    ),
                    "estimated_cost_usd": (
                        account.reserved.estimated_cost_usd + request.estimated_cost_usd
                    ),
                }
            )
            self._store.save_account(account.model_copy(update={"reserved": updated_reserved}))
            pending = UsageRecord(
                attempt_id=attempt_id,
                job_id=request.job_id,
                scope_id=request.scope_id,
                parent_tool_call_id=request.parent_tool_call_id,
                provider=request.provider,
                model_id=request.model_id,
                status="pending",
                reserved_images=request.image_count,
                images_sent=0,
                estimated_cost_usd=request.estimated_cost_usd,
                confirmed_cost_usd=None,
                cost_status="estimated",
            )
            if self._durable:
                self._durable.insert_attempt(
                    reservation,
                    pending,
                    operation_key=operation_key,
                    explicit_retry=explicit_retry,
                    previous_attempt_id=previous_attempt_id,
                    price_version=self._prices.effective_date,
                )
            else:
                self._store.record_usage(pending)
                self._active_reservations[attempt_id] = _ActiveAttempt(reservation, pending)
            return reservation

    def _release_reserved_counters(
        self,
        reserved: BudgetCounters,
        reservation: BudgetReservation,
    ) -> BudgetCounters:
        return reserved.model_copy(
            update={
                "provider_attempts": max(0, reserved.provider_attempts - 1),
                "submitted_image_parts": max(
                    0,
                    reserved.submitted_image_parts - reservation.reserved_images,
                ),
                "estimated_cost_usd": max(
                    0.0,
                    reserved.estimated_cost_usd - reservation.reserved_cost_usd,
                ),
            }
        )

    def _active_attempt_locked(self, attempt_id: str | None) -> _ActiveAttempt:
        if self._durable and attempt_id:
            row = self._durable.read_attempt(attempt_id)
            if row and row[1].status == "pending":
                return _ActiveAttempt(*row)
        active = self._active_reservations.get(attempt_id) if attempt_id is not None else None
        if active is None:
            raise BudgetError(
                ErrorCode.BUDGET_EXCEEDED,
                "provider request requires an active reservation",
            )
        return active

    def prepare_vision_send(
        self, request: VisionRequest, *, provider: str, model_id: str
    ) -> VisionRequest:
        return self.prepare_provider_send(request, provider=provider, model_id=model_id)

    def prepare_provider_send[T: (VisionRequest, PlannerRequest)](
        self, request: T, *, provider: str, model_id: str
    ) -> T:
        """Bind one reservation to the outgoing payload before either transport may run."""
        with self._atomic():
            attempt = self._active_attempt_locked(request.attempt_id)
            reservation = attempt.reservation
            account = self._load_account_locked(reservation.scope_id)
            remaining = self._check_active_locked(account)
            if self._durable:
                self._durable.check_binding(
                    reservation.scope_id, request.job_id, request.asset_id, request.analysis_id
                )
            if isinstance(request, PlannerRequest):
                wrong_scope = account.scope_kind != "qa" or account.scope_id != request.question_id
            else:
                wrong_scope = (
                    account.scope_kind == "analysis" and account.scope_id != request.analysis_id
                )
            if (
                reservation.job_id != request.job_id
                or reservation.provider != provider
                or reservation.model_id != model_id
                or wrong_scope
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED, "provider request does not match reservation"
                )
            if attempt.dispatched:
                raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "provider attempt already dispatched")

            timeout = clamp_request_timeout_seconds(request.timeout_seconds, remaining)
            update: dict[str, object] = {
                "timeout_seconds": min(timeout, reservation.timeout_seconds)
            }
            image_count = 0
            if isinstance(request, VisionRequest):
                images = request.images.copy()
                image_count = len(images)
                if not 1 <= image_count <= reservation.reserved_images:
                    raise BudgetError(
                        ErrorCode.BUDGET_EXCEEDED, "vision payload outside reserved image count"
                    )
                update["images"] = images
            outgoing = request.model_copy(update=update)
            pending = attempt.pending.model_copy(
                update={
                    "images_sent": image_count,
                    "cost_status": "unknown" if self._durable else attempt.pending.cost_status,
                }
            )
            self._store.record_usage(pending, dispatched=True)
            attempt.pending = pending
            attempt.dispatched = True
            return outgoing

    def _already_settled(
        self,
        reservation: BudgetReservation,
        status: str,
        **fields: object,
    ) -> bool:
        if not self._durable:
            return False
        row = self._durable.read_attempt(reservation.attempt_id)
        if row is None or row[1].status == "pending":
            return False
        saved, record, _ = row
        if (
            saved != reservation
            or record.status != status
            or any(
                getattr(record, name) != value
                for name, value in fields.items()
                if value is not None
            )
        ):
            raise BudgetError(ErrorCode.IDEMPOTENCY_CONFLICT, "attempt settlement differs")
        return True

    def settle_success(
        self,
        reservation: BudgetReservation,
        *,
        input_tokens: int,
        output_tokens: int,
        images_sent: int | None = None,
        provider_request_id: str | None = None,
        confirmed_cost_usd: float | None = None,
    ) -> None:
        """Atomically validate and settle a provider attempt; errors leave reservation active."""
        with self._atomic():
            if self._already_settled(
                reservation,
                "succeeded",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                images_sent=images_sent,
                confirmed_cost_usd=confirmed_cost_usd,
                provider_request_id=provider_request_id,
            ):
                return
            attempt = self._active_attempt_locked(reservation.attempt_id)
            active = attempt.reservation
            account = self._load_account_locked(active.scope_id)

            if (
                isinstance(input_tokens, bool)
                or not isinstance(input_tokens, int)
                or input_tokens < 0
                or isinstance(output_tokens, bool)
                or not isinstance(output_tokens, int)
                or output_tokens < 0
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    "input_tokens and output_tokens must be non-negative integers",
                )

            actual_images = attempt.chargeable_images if images_sent is None else images_sent
            if (
                isinstance(actual_images, bool)
                or not isinstance(actual_images, int)
                or actual_images < 0
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    "images_sent must be a non-negative integer",
                )
            if actual_images > active.reserved_images:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    (
                        f"images_sent ({actual_images}) exceeds reserved_images "
                        f"({active.reserved_images})"
                    ),
                )
            if attempt.dispatched and actual_images != attempt.pending.images_sent:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED, "reported images differ from dispatched payload"
                )
            if actual_images > account.limits.max_images_per_request:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    (
                        f"images_sent ({actual_images}) exceeds max_images_per_request "
                        f"({account.limits.max_images_per_request})"
                    ),
                )
            if (
                account.consumed.submitted_image_parts + actual_images
                > account.limits.max_submitted_image_parts
            ):
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED,
                    (
                        f"images_sent ({actual_images}) would exceed max_submitted_image_parts "
                        f"({account.limits.max_submitted_image_parts})"
                    ),
                )

            if confirmed_cost_usd is not None:
                cost_status: CostStatus = "confirmed"
                est_cost = confirmed_cost_usd
                conf_cost: float | None = confirmed_cost_usd
            elif active.model_id in self._prices.models:
                cost_status = "estimated"
                est_cost = estimate_cost_usd(
                    self._prices,
                    active.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    image_count=0,
                )
                conf_cost = None
            else:
                cost_status = "estimated"
                est_cost = active.reserved_cost_usd
                conf_cost = None

            updated_reserved = self._release_reserved_counters(account.reserved, active)
            updated_consumed = account.consumed.model_copy(
                update={
                    "provider_attempts": account.consumed.provider_attempts + 1,
                    "submitted_image_parts": (
                        account.consumed.submitted_image_parts + actual_images
                    ),
                    "estimated_cost_usd": account.consumed.estimated_cost_usd + est_cost,
                    "confirmed_cost_usd": (
                        account.consumed.confirmed_cost_usd + (conf_cost or 0.0)
                    ),
                }
            )
            next_account = account.model_copy(
                update={"reserved": updated_reserved, "consumed": updated_consumed}
            )
            usage_record = UsageRecord(
                attempt_id=active.attempt_id,
                job_id=active.job_id,
                scope_id=active.scope_id,
                parent_tool_call_id=active.parent_tool_call_id,
                provider=active.provider,
                model_id=active.model_id,
                status="succeeded",
                reserved_images=active.reserved_images,
                images_sent=actual_images,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=est_cost,
                confirmed_cost_usd=conf_cost,
                cost_status=cost_status,
                provider_request_id=provider_request_id,
            )

            self._store.save_account(next_account)
            self._store.record_usage(usage_record)
            self._active_reservations.pop(active.attempt_id, None)

    def settle_not_sent(
        self,
        reservation: BudgetReservation,
        *,
        error_code: ErrorCode,
    ) -> None:
        """Release reservation when a failure occurred strictly BEFORE sending over the wire."""
        with self._atomic():
            if self._already_settled(reservation, "not_sent", error_code=error_code.value):
                return
            attempt = self._active_attempt_locked(reservation.attempt_id)
            active = attempt.reservation
            if attempt.dispatched:
                raise BudgetError(
                    ErrorCode.BUDGET_EXCEEDED, "cannot refund a dispatched provider request"
                )
            account = self._load_account_locked(active.scope_id)
            updated_reserved = self._release_reserved_counters(account.reserved, active)
            next_account = account.model_copy(update={"reserved": updated_reserved})
            usage_record = UsageRecord(
                attempt_id=active.attempt_id,
                job_id=active.job_id,
                scope_id=active.scope_id,
                parent_tool_call_id=active.parent_tool_call_id,
                provider=active.provider,
                model_id=active.model_id,
                status="not_sent",
                reserved_images=active.reserved_images,
                images_sent=0,
                estimated_cost_usd=0.0,
                confirmed_cost_usd=0.0,
                cost_status="confirmed",
                error_code=error_code.value,
            )
            self._store.save_account(next_account)
            self._store.record_usage(usage_record)
            self._active_reservations.pop(active.attempt_id, None)

    def settle_failed(
        self,
        reservation: BudgetReservation,
        *,
        error_code: ErrorCode,
        confirmed_cost_usd: float | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        """Settle a sent attempt that returned an explicit provider error (attempt/images spent)."""
        with self._atomic():
            if self._already_settled(
                reservation,
                "failed",
                error_code=error_code.value,
                confirmed_cost_usd=confirmed_cost_usd,
                provider_request_id=provider_request_id,
            ):
                return
            attempt = self._active_attempt_locked(reservation.attempt_id)
            active = attempt.reservation
            account = self._load_account_locked(active.scope_id)
            updated_reserved = self._release_reserved_counters(account.reserved, active)

            if confirmed_cost_usd is not None:
                cost_status: CostStatus = "confirmed"
                est_cost = confirmed_cost_usd
                conf_cost: float | None = confirmed_cost_usd
            else:
                cost_status = "estimated"
                est_cost = active.reserved_cost_usd
                conf_cost = None

            updated_consumed = account.consumed.model_copy(
                update={
                    "provider_attempts": account.consumed.provider_attempts + 1,
                    "submitted_image_parts": (
                        account.consumed.submitted_image_parts + attempt.chargeable_images
                    ),
                    "estimated_cost_usd": account.consumed.estimated_cost_usd + est_cost,
                    "confirmed_cost_usd": (
                        account.consumed.confirmed_cost_usd + (conf_cost or 0.0)
                    ),
                }
            )
            next_account = account.model_copy(
                update={"reserved": updated_reserved, "consumed": updated_consumed}
            )
            usage_record = UsageRecord(
                attempt_id=active.attempt_id,
                job_id=active.job_id,
                scope_id=active.scope_id,
                parent_tool_call_id=active.parent_tool_call_id,
                provider=active.provider,
                model_id=active.model_id,
                status="failed",
                reserved_images=active.reserved_images,
                images_sent=attempt.chargeable_images,
                estimated_cost_usd=est_cost,
                confirmed_cost_usd=conf_cost,
                cost_status=cost_status,
                provider_request_id=provider_request_id,
                error_code=error_code.value,
            )
            self._store.save_account(next_account)
            self._store.record_usage(usage_record)
            self._active_reservations.pop(active.attempt_id, None)

    def settle_unknown(
        self,
        reservation: BudgetReservation,
        *,
        error_code: ErrorCode = ErrorCode.EXTERNAL_RESULT_UNKNOWN,
    ) -> None:
        """Settle a sent attempt with unknown remote outcome (never treated as zero or refunded)."""
        with self._atomic():
            if self._already_settled(reservation, "unknown", error_code=error_code.value):
                return
            attempt = self._active_attempt_locked(reservation.attempt_id)
            active = attempt.reservation
            account = self._load_account_locked(active.scope_id)
            updated_reserved = self._release_reserved_counters(account.reserved, active)
            updated_consumed = account.consumed.model_copy(
                update={
                    "provider_attempts": account.consumed.provider_attempts + 1,
                    "submitted_image_parts": (
                        account.consumed.submitted_image_parts + attempt.chargeable_images
                    ),
                    "estimated_cost_usd": (
                        account.consumed.estimated_cost_usd + active.reserved_cost_usd
                    ),
                }
            )
            next_account = account.model_copy(
                update={
                    "reserved": updated_reserved,
                    "consumed": updated_consumed,
                    "has_unknown_external_result": True,
                }
            )
            usage_record = UsageRecord(
                attempt_id=active.attempt_id,
                job_id=active.job_id,
                scope_id=active.scope_id,
                parent_tool_call_id=active.parent_tool_call_id,
                provider=active.provider,
                model_id=active.model_id,
                status="unknown",
                reserved_images=active.reserved_images,
                images_sent=attempt.chargeable_images,
                estimated_cost_usd=active.reserved_cost_usd,
                confirmed_cost_usd=None,
                cost_status="unknown",
                error_code=error_code.value,
            )
            self._store.save_account(next_account)
            self._store.record_usage(usage_record)
            self._active_reservations.pop(active.attempt_id, None)

    def get_account(self, scope_id: str) -> BudgetAccount:
        """Return current snapshot of the budget account."""
        with self._atomic():
            return self._load_account_locked(scope_id)

    def get_ledger(self, scope_id: str) -> UsageLedger:
        """Aggregate reserved vs settled usage records and overall cost status for the scope."""
        with self._atomic():
            snapshot = self._store.list_usage_with_dispatch(scope_id)
            records = [rec for rec, _ in snapshot]
            pending_records = [rec for rec in records if rec.status == "pending"]
            sent_records = [
                rec
                for rec, dispatched in snapshot
                if rec.status in {"succeeded", "failed", "unknown"}
                or (rec.status == "pending" and (dispatched or rec.images_sent > 0))
            ]
            reserved_attempts = len(pending_records)
            reserved_images = sum(rec.reserved_images for rec in pending_records)
            total_attempts = len(sent_records)
            total_images = sum(rec.images_sent for rec in sent_records)
            confirmed_cost = sum(rec.confirmed_cost_usd or 0.0 for rec in sent_records)
            estimated_cost = sum(rec.estimated_cost_usd for rec in sent_records)

            if any(rec.cost_status == "unknown" for rec in sent_records):
                overall_status: CostStatus = "unknown"
            elif pending_records or any(rec.cost_status == "estimated" for rec in sent_records):
                overall_status = "estimated"
            else:
                overall_status = "confirmed"

            return UsageLedger(
                scope_id=scope_id,
                records=records,
                reserved_attempts=reserved_attempts,
                reserved_images=reserved_images,
                total_attempts=total_attempts,
                total_images_sent=total_images,
                confirmed_cost_usd=confirmed_cost,
                estimated_cost_usd=estimated_cost,
                cost_status=overall_status,
            )


class BudgetedVisionProvider:
    """Apply the shared budget immediately before invoking the injected vision port."""

    def __init__(self, provider: VisionProvider, guard: BudgetGuard) -> None:
        self._provider = provider
        self._guard = guard
        self.provider_name = provider.provider_name
        self.model_id = provider.model_id

    async def observe(self, request: VisionRequest) -> VisionResponse:
        outgoing = self._guard.prepare_vision_send(
            request, provider=self._provider.provider_name, model_id=self._provider.model_id
        )
        return await self._provider.observe(outgoing)


class BudgetedPlannerProvider:
    """Claim a QA attempt on the shared guard before handing a decision to the planner."""

    def __init__(self, provider: PlannerProvider, guard: BudgetGuard) -> None:
        self._provider = provider
        self._guard = guard
        self.provider_name = provider.provider_name
        self.model_id = provider.model_id

    async def decide(self, request: PlannerRequest) -> PlannerResponse:
        outgoing = self._guard.prepare_provider_send(
            request, provider=self._provider.provider_name, model_id=self._provider.model_id
        )
        return await self._provider.decide(outgoing)
