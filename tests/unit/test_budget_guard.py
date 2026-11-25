"""Unit tests for M1.7 in-memory BudgetGuard, BudgetStore, and single-source policy."""

from __future__ import annotations

import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from video_agent.adapters.llm.fake import FakeLlmScenario, FakePlannerProvider, FakeVisionProvider
from video_agent.adapters.storage.memory_budget import InMemoryBudgetStore
from video_agent.application.budget import BudgetGuard
from video_agent.bootstrap import build_offline_container, load_app_config, load_price_config
from video_agent.domain.errors import BudgetError, ErrorCode, ProviderError
from video_agent.domain.models import BudgetLimits, RequestBudget
from video_agent.domain.ports import BudgetStore, ImageInput, PlannerRequest, VisionRequest

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    """Deterministic controllable UTC + monotonic clock for deadline & skew tests."""

    def __init__(self, start_utc: datetime | None = None, start_mono: float = 1000.0) -> None:
        self._utc = start_utc or datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
        self._mono = start_mono

    def now_utc(self) -> datetime:
        return self._utc

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._utc = self._utc + timedelta(seconds=seconds)
        self._mono += seconds

    def jump_wall_backwards(self, seconds: float) -> None:
        self._utc = self._utc - timedelta(seconds=seconds)


def _run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        coro.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    else:
        coro.close()
        raise AssertionError("fake coroutine suspended unexpectedly")


def _make_guard(
    *,
    store: InMemoryBudgetStore | None = None,
    clock: FakeClock | None = None,
) -> tuple[BudgetGuard, InMemoryBudgetStore, FakeClock]:
    actual_store = store if store is not None else InMemoryBudgetStore()
    actual_clock = clock if clock is not None else FakeClock()
    config = load_app_config(REPO_ROOT / "config" / "defaults.yaml")
    prices = load_price_config(REPO_ROOT / "config" / "prices.yaml")
    guard = BudgetGuard(store=actual_store, clock=actual_clock, config=config, prices=prices)
    return guard, actual_store, actual_clock


def test_memory_store_implements_budget_store_protocol() -> None:
    """InMemoryBudgetStore satisfies domain.ports.BudgetStore."""
    assert isinstance(InMemoryBudgetStore(), BudgetStore)


def test_tool_layer_and_provider_boundary_do_not_double_count_and_count_retries() -> None:
    """Tech Arch 9.2 case: 1 inspect_clip + 1 retry (8+8 imgs) = 1 tool, 2 attempts, 16 imgs."""
    guard, _, _ = _make_guard()
    guard.ensure_started("q_001", "qa")

    tool_call_id = guard.record_tool_execution("q_001", tool_name="inspect_clip", expensive=True)

    # First attempt sends 8 images and hits rate limit -> retry consumes a second attempt + 8 images
    res1 = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="q_001",
            job_id="job_qa_1",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=8,
            estimated_cost_usd=0.01,
            parent_tool_call_id=tool_call_id,
        )
    )
    guard.settle_failed(res1, error_code=ErrorCode.RATE_LIMITED, confirmed_cost_usd=0.0)

    res2 = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="q_001",
            job_id="job_qa_1",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=8,
            estimated_cost_usd=0.01,
            parent_tool_call_id=tool_call_id,
        )
    )
    guard.settle_success(
        res2,
        input_tokens=8500,
        output_tokens=200,
        images_sent=8,
        provider_request_id="req_ok_2",
        confirmed_cost_usd=0.009,
    )

    acc = guard.get_account("q_001")
    assert acc.consumed.tool_calls == 1
    assert acc.consumed.expensive_tool_calls == 1
    assert acc.consumed.provider_attempts == 2
    assert acc.consumed.submitted_image_parts == 16
    assert acc.reserved.provider_attempts == 0
    assert acc.reserved.submitted_image_parts == 0

    ledger = guard.get_ledger("q_001")
    assert ledger.total_attempts == 2
    assert ledger.total_images_sent == 16
    assert all(rec.parent_tool_call_id == tool_call_id for rec in ledger.records)


def test_multiple_tools_in_one_round_counted_individually_and_third_rejected() -> None:
    """Each tool in a multi-tool round is counted; exceeding tool cap fails before execution."""
    guard, _, _ = _make_guard()
    tight_limits = BudgetLimits(
        scope_kind="qa",
        max_rounds=5,
        max_tool_calls=2,
        max_expensive_tool_calls=1,
        max_submitted_image_parts=32,
        max_images_per_request=16,
        max_provider_attempts=10,
        deadline_seconds=180,
        reserve_final_synthesis_attempt=True,
    )
    guard.ensure_started("q_multi", "qa", limits=tight_limits)

    assert guard.begin_round("q_multi") == 1
    guard.record_tool_execution("q_multi", tool_name="search_evidence", expensive=False)
    guard.record_tool_execution("q_multi", tool_name="inspect_clip", expensive=True)

    with pytest.raises(BudgetError) as exc_tool:
        guard.record_tool_execution("q_multi", tool_name="search_evidence", expensive=False)
    assert exc_tool.value.code is ErrorCode.BUDGET_EXCEEDED
    assert guard.get_account("q_multi").consumed.tool_calls == 2


def test_not_sent_releases_reservation_while_unknown_never_refunds_or_zeros_cost() -> None:
    """Pre-send failure refunds slot; sent-then-unknown keeps attempts, images, unknown cost."""
    guard, _, _ = _make_guard()
    guard.ensure_started("an_001", "analysis")

    # Pre-send failure releases reservation cleanly
    res_pre = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="an_001",
            job_id="job_1",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=4,
            estimated_cost_usd=0.02,
        )
    )
    guard.settle_not_sent(res_pre, error_code=ErrorCode.CREDENTIAL_MISSING)
    acc_after_pre = guard.get_account("an_001")
    assert acc_after_pre.consumed.provider_attempts == 0
    assert acc_after_pre.consumed.submitted_image_parts == 0
    assert acc_after_pre.reserved.provider_attempts == 0

    # Sent-then-unknown keeps attempt + images consumed and marks ledger cost_status="unknown"
    res_unk = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="an_001",
            job_id="job_1",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=8,
            estimated_cost_usd=0.05,
        )
    )
    guard.settle_unknown(res_unk, error_code=ErrorCode.EXTERNAL_RESULT_UNKNOWN)
    acc_after_unk = guard.get_account("an_001")
    assert acc_after_unk.consumed.provider_attempts == 1
    assert acc_after_unk.consumed.submitted_image_parts == 8
    assert acc_after_unk.has_unknown_external_result is True

    ledger = guard.get_ledger("an_001")
    assert ledger.cost_status == "unknown"
    assert ledger.records[-1].cost_status == "unknown"
    assert ledger.records[-1].estimated_cost_usd == pytest.approx(0.05)


def test_final_synthesis_reservation_and_concurrent_last_slot_atomicity() -> None:
    """QA reserves 1 final synthesis slot; concurrent threads racing for last slot allow only 1."""
    guard, _, _ = _make_guard()
    limits = BudgetLimits(
        scope_kind="qa",
        max_rounds=5,
        max_tool_calls=6,
        max_expensive_tool_calls=2,
        max_submitted_image_parts=16,
        max_images_per_request=8,
        max_provider_attempts=2,
        deadline_seconds=180,
        reserve_final_synthesis_attempt=True,
    )
    guard.ensure_started("q_race", "qa", limits=limits)

    # 2 concurrent threads race for the ONLY non-synthesis slot (1 of 2; slot 2 is reserved)
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        try:
            guard.reserve_provider_attempt(
                RequestBudget(
                    scope_id="q_race",
                    job_id="job_q_race",
                    provider="deepseek",
                    model_id="deepseek-flash",
                    image_count=2,
                    estimated_cost_usd=0.01,
                    is_final_synthesis=False,
                )
            )
            outcome = "ok"
        except BudgetError as exc:
            outcome = exc.code.value
        with lock:
            results.append(outcome)

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert sorted(results) == [ErrorCode.BUDGET_EXCEEDED.value, "ok"]

    # Now only the reserved final synthesis slot remains: non-final fails, final succeeds
    with pytest.raises(BudgetError) as exc_non_final:
        guard.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_race",
                job_id="job_q_race",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=0,
                is_final_synthesis=False,
            )
        )
    assert exc_non_final.value.code is ErrorCode.BUDGET_EXCEEDED

    final_res = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="q_race",
            job_id="job_q_race",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=0,
            is_final_synthesis=True,
        )
    )
    guard.settle_success(final_res, input_tokens=100, output_tokens=50, images_sent=0)

    # After 0 attempts remain, even final synthesis cannot sneak in an extra call
    with pytest.raises(BudgetError) as exc_after_zero:
        guard.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_race",
                job_id="job_q_race",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=0,
                is_final_synthesis=True,
            )
        )
    assert exc_after_zero.value.code is ErrorCode.BUDGET_EXCEEDED


def test_deadline_persists_across_restart_clamps_timeout_and_detects_cancel_and_skew() -> None:
    """Restart does not reset counters/deadline; timeout clamps; cancel & clock skew stop calls."""
    guard1, store, clock = _make_guard()
    acc1 = guard1.ensure_started("q_life", "qa")
    first_deadline = acc1.deadline_at_utc
    assert first_deadline is not None

    # Advance 160s of 180s QA deadline -> remaining is 20s (< 45s configured timeout)
    clock.advance(160.0)

    # Simulate process restart with a new BudgetGuard over the same BudgetStore
    guard2, _, _ = _make_guard(store=store, clock=clock)
    acc2 = guard2.ensure_started("q_life", "qa")
    assert acc2.deadline_at_utc == first_deadline

    res = guard2.reserve_provider_attempt(
        RequestBudget(
            scope_id="q_life",
            job_id="job_life",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=0,
        )
    )
    assert res.timeout_seconds == pytest.approx(20.0)
    guard2.settle_success(res, input_tokens=50, output_tokens=10, images_sent=0)

    # Per-request image cap rejects > 16 images
    with pytest.raises(BudgetError) as exc_per_req:
        guard2.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_life",
                job_id="job_life",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=17,
            )
        )
    assert exc_per_req.value.code is ErrorCode.BUDGET_EXCEEDED

    # Wall clock jumping backwards by 30s triggers CLOCK_SKEW_DETECTED instead of extending deadline
    clock.jump_wall_backwards(30.0)
    with pytest.raises(BudgetError) as exc_skew:
        guard2.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_life",
                job_id="job_life",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=0,
            )
        )
    assert exc_skew.value.code is ErrorCode.CLOCK_SKEW_DETECTED

    # Fresh scope: expiration and cancellation both block new external calls
    guard3, _, clock3 = _make_guard()
    guard3.ensure_started("q_exp", "qa")
    clock3.advance(181.0)
    with pytest.raises(BudgetError) as exc_exp:
        guard3.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_exp",
                job_id="job_exp",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=0,
            )
        )
    assert exc_exp.value.code is ErrorCode.DEADLINE_EXCEEDED

    guard3.ensure_started("q_can", "qa")
    guard3.cancel("q_can")
    with pytest.raises(BudgetError) as exc_can:
        guard3.reserve_provider_attempt(
            RequestBudget(
                scope_id="q_can",
                job_id="job_can",
                provider="deepseek",
                model_id="deepseek-flash",
                image_count=0,
            )
        )
    assert exc_can.value.code is ErrorCode.JOB_CANCELLED


def test_budget_guard_tracks_all_six_fake_provider_scenarios() -> None:
    """All 6 fake scenarios (normal/schema/fabricated/timeout/rate_limit/unknown) settle cleanly."""
    scenarios: list[FakeLlmScenario] = [
        "normal",
        "schema_error",
        "fabricated_citation",
        "timeout",
        "rate_limit",
        "unknown_result",
    ]
    fake = FakeVisionProvider(scenarios=scenarios)
    guard, _, _ = _make_guard()
    guard.ensure_started("an_scenarios", "analysis")

    for idx in range(len(scenarios)):
        res = guard.reserve_provider_attempt(
            RequestBudget(
                scope_id="an_scenarios",
                job_id="job_sc",
                provider=fake.provider_name,
                model_id=fake.model_id,
                image_count=2,
                estimated_cost_usd=0.005,
            )
        )
        req = VisionRequest(
            asset_id="asset_1",
            analysis_id="an_scenarios",
            job_id="job_sc",
            prompt_text=f"Window {idx}",
            prompt_bundle_hash="h" * 64,
            images=[
                ImageInput(
                    evidence_id="ev_1",
                    artifact_id="art_1",
                    width=1920,
                    height=1080,
                    actual_pts_ms=0,
                ),
                ImageInput(
                    evidence_id="ev_2",
                    artifact_id="art_2",
                    width=1920,
                    height=1080,
                    actual_pts_ms=5000,
                ),
            ],
            timeout_seconds=res.timeout_seconds,
            attempt_id=res.attempt_id,
        )
        try:
            resp = _run_coro(fake.observe(req))
            guard.settle_success(
                res,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                images_sent=len(req.images),
                provider_request_id=resp.provider_request_id,
                confirmed_cost_usd=0.0,
            )
        except ProviderError as exc:
            if exc.code in {ErrorCode.PROVIDER_TIMEOUT, ErrorCode.EXTERNAL_RESULT_UNKNOWN}:
                guard.settle_unknown(res, error_code=exc.code)
            else:
                guard.settle_failed(res, error_code=exc.code, confirmed_cost_usd=0.0)

    ledger = guard.get_ledger("an_scenarios")
    assert ledger.total_attempts == 6
    assert ledger.total_images_sent == 12
    assert ledger.cost_status == "unknown"
    assert guard.get_account("an_scenarios").has_unknown_external_result is True


def test_reserved_and_sent_images_counted_separately_and_settlement_failure_allows_unknown() -> (
    None
):
    """M1.7 regression: reserved vs sent split, settle image cap, atomic failure recovery."""
    guard, _, _ = _make_guard()
    limits = BudgetLimits(
        scope_kind="qa",
        max_rounds=5,
        max_tool_calls=6,
        max_expensive_tool_calls=2,
        max_submitted_image_parts=1,
        max_images_per_request=1,
        max_provider_attempts=2,
        deadline_seconds=180,
        reserve_final_synthesis_attempt=False,
    )
    guard.ensure_started("q_strict_img", "qa", limits=limits)

    res = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="q_strict_img",
            job_id="job_strict_1",
            provider="deepseek",
            model_id="deepseek-flash",
            image_count=1,
            estimated_cost_usd=0.01,
        )
    )

    # While only reserved (pending), ledger MUST distinguish reserved vs sent (sent == 0)
    pending_acc = guard.get_account("q_strict_img")
    assert pending_acc.reserved.submitted_image_parts == 1
    assert pending_acc.consumed.submitted_image_parts == 0

    pending_ledger = guard.get_ledger("q_strict_img")
    assert pending_ledger.total_attempts == 0
    assert pending_ledger.total_images_sent == 0
    assert getattr(pending_ledger, "reserved_attempts", None) == 1
    assert getattr(pending_ledger, "reserved_images", None) == 1
    assert pending_ledger.records[-1].status == "pending"
    assert pending_ledger.records[-1].images_sent == 0

    # Settling with images_sent=2 when only 1 was reserved (and cap is 1) MUST fail atomically
    with pytest.raises(BudgetError) as exc_img:
        guard.settle_success(
            res,
            input_tokens=100,
            output_tokens=20,
            images_sent=2,
        )
    assert exc_img.value.code is ErrorCode.BUDGET_EXCEEDED

    # Invalid token count MUST also fail atomically without destroying the active reservation
    with pytest.raises((BudgetError, ValueError)):
        guard.settle_success(
            res,
            input_tokens=-10,
            output_tokens=20,
            images_sent=1,
        )

    still_pending_acc = guard.get_account("q_strict_img")
    assert still_pending_acc.reserved.provider_attempts == 1
    assert still_pending_acc.reserved.submitted_image_parts == 1
    assert still_pending_acc.consumed.provider_attempts == 0
    assert still_pending_acc.consumed.submitted_image_parts == 0

    # After failed settlement attempts, recording unknown outcome MUST still succeed cleanly
    guard.settle_unknown(res, error_code=ErrorCode.EXTERNAL_RESULT_UNKNOWN)

    final_acc = guard.get_account("q_strict_img")
    assert final_acc.reserved.provider_attempts == 0
    assert final_acc.reserved.submitted_image_parts == 0
    assert final_acc.consumed.provider_attempts == 1
    assert final_acc.consumed.submitted_image_parts == 1
    assert final_acc.has_unknown_external_result is True

    final_ledger = guard.get_ledger("q_strict_img")
    assert final_ledger.total_attempts == 1
    assert final_ledger.total_images_sent == 1
    assert getattr(final_ledger, "reserved_attempts", None) == 0
    assert getattr(final_ledger, "reserved_images", None) == 0
    assert final_ledger.cost_status == "unknown"
    assert final_ledger.records[-1].status == "unknown"


def test_composed_vision_checks_actual_payload_before_dispatch_and_reuse() -> None:
    fake = FakeVisionProvider()
    clock = FakeClock()
    app = build_offline_container(vision_provider=fake, clock=clock)
    image = ImageInput(evidence_id="ev", artifact_id="art", width=10, height=8, actual_pts_ms=4)
    request = VisionRequest(
        asset_id="asset",
        analysis_id="analysis",
        job_id="job",
        images=[image, image],
        prompt_text="Observe",
        prompt_bundle_hash="h",
        timeout_seconds=90.0,
    )
    for attempt_id in (None, "unknown"):
        with pytest.raises(BudgetError) as rejected:
            _run_coro(
                app.vision_provider.observe(request.model_copy(update={"attempt_id": attempt_id}))
            )
        assert rejected.value.code is ErrorCode.BUDGET_EXCEEDED
    assert not fake.calls

    guard = app.budget_guard
    limits = BudgetLimits(
        scope_kind="analysis",
        max_images_per_request=1,
        max_submitted_image_parts=1,
        max_provider_attempts=1,
        deadline_seconds=20,
    )
    guard.ensure_started("analysis", "analysis", limits=limits)
    budget = RequestBudget(
        scope_id="analysis",
        job_id="job",
        provider=fake.provider_name,
        model_id=fake.model_id,
        image_count=1,
    )
    reservation = guard.reserve_provider_attempt(budget)
    bound = request.model_copy(update={"attempt_id": reservation.attempt_id})
    correct = bound.model_copy(update={"images": [image]})
    for invalid in (
        bound,
        correct.model_copy(update={"job_id": "other"}),
        correct.model_copy(update={"analysis_id": "other"}),
    ):
        with pytest.raises(BudgetError) as rejected:
            _run_coro(app.vision_provider.observe(invalid))
        assert rejected.value.code is ErrorCode.BUDGET_EXCEEDED
    assert not fake.calls
    assert guard.get_ledger("analysis").total_images_sent == 0

    clock.advance(15.0)
    _run_coro(app.vision_provider.observe(correct))
    correct.images.append(image)
    assert len(fake.calls) == 1
    assert fake.calls[0].images == [image]
    assert fake.calls[0].timeout_seconds == pytest.approx(5.0)
    assert guard.get_ledger("analysis").total_images_sent == 1
    retry = bound.model_copy(update={"images": [image]})
    with pytest.raises(BudgetError):
        _run_coro(app.vision_provider.observe(retry))
    with pytest.raises(BudgetError):
        guard.settle_not_sent(reservation, error_code=ErrorCode.CREDENTIAL_MISSING)
    with pytest.raises(BudgetError):
        guard.settle_success(reservation, input_tokens=1, output_tokens=1, images_sent=0)
    guard.settle_success(reservation, input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetError):
        _run_coro(app.vision_provider.observe(retry))
    assert len(fake.calls) == 1
    assert guard.get_ledger("analysis").total_attempts == 1
    assert guard.get_account("analysis").consumed.submitted_image_parts == 1


@pytest.mark.parametrize("scenario", ["normal", "rate_limit", "unknown_result"])
def test_composed_vision_charges_only_forwarded_images(scenario: FakeLlmScenario) -> None:
    fake = FakeVisionProvider(scenarios=[scenario])
    app = build_offline_container(vision_provider=fake)
    image = ImageInput(evidence_id="ev", artifact_id="art", width=10, height=8, actual_pts_ms=4)
    request = VisionRequest(
        asset_id="asset",
        analysis_id="analysis",
        job_id="qa_job",
        images=[image],
        prompt_text="Observe",
        prompt_bundle_hash="h",
    )
    with pytest.raises(BudgetError):
        _run_coro(app.vision_provider.observe(request))
    guard = app.budget_guard
    guard.ensure_started("question", "qa")
    reservation = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id="question",
            job_id="qa_job",
            provider=fake.provider_name,
            model_id=fake.model_id,
            image_count=2,
            estimated_cost_usd=0.02,
        )
    )
    bound = request.model_copy(update={"attempt_id": reservation.attempt_id})
    if scenario == "normal":
        response = _run_coro(app.vision_provider.observe(bound))
        guard.settle_success(
            reservation, input_tokens=response.input_tokens, output_tokens=response.output_tokens
        )
    else:
        with pytest.raises(ProviderError) as err:
            _run_coro(app.vision_provider.observe(bound))
        if scenario == "unknown_result":
            guard.settle_unknown(reservation, error_code=err.value.code)
            assert guard.get_ledger("question").cost_status == "unknown"
        else:
            guard.settle_failed(reservation, error_code=err.value.code)

    assert len(fake.calls) == 1
    ledger = guard.get_ledger("question")
    assert (ledger.total_attempts, ledger.total_images_sent, ledger.reserved_images) == (1, 1, 0)
    assert (ledger.records[0].reserved_images, ledger.records[0].images_sent) == (2, 1)
    assert guard.get_account("question").consumed.submitted_image_parts == 1


def _planner_request() -> PlannerRequest:
    return PlannerRequest(
        asset_id="asset",
        analysis_id="analysis",
        job_id="qa-job",
        question_id="audit-q",
        prompt_text="Decide",
        prompt_bundle_hash="h",
        timeout_seconds=90.0,
    )


def _planner_budget(fake: FakePlannerProvider) -> RequestBudget:
    return RequestBudget(
        scope_id="audit-q",
        job_id="qa-job",
        provider=fake.provider_name,
        model_id=fake.model_id,
        estimated_cost_usd=0.03,
    )


def test_composed_planner_requires_a_reservation_even_in_the_default_container() -> None:
    fake = FakePlannerProvider()
    request = _planner_request()
    for app in (build_offline_container(planner_provider=fake), build_offline_container()):
        guard = app.budget_guard
        guard.ensure_started("audit-q", "qa")
        guard.cancel("audit-q")
        for attempt_id in (None, "unknown"):
            with pytest.raises(BudgetError):
                _run_coro(
                    app.planner_provider.decide(
                        request.model_copy(update={"attempt_id": attempt_id})
                    )
                )
        ledger = guard.get_ledger("audit-q")
        assert ledger.total_attempts == 0
        assert ledger.records == []
    assert fake.calls == []


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("cancelled", ErrorCode.JOB_CANCELLED),
        ("expired", ErrorCode.DEADLINE_EXCEEDED),
        ("provider", ErrorCode.BUDGET_EXCEEDED),
        ("model_id", ErrorCode.BUDGET_EXCEEDED),
        ("job_id", ErrorCode.BUDGET_EXCEEDED),
        ("question_id", ErrorCode.BUDGET_EXCEEDED),
        ("analysis_scope", ErrorCode.BUDGET_EXCEEDED),
    ],
)
def test_composed_planner_rejects_before_transport(case: str, code: ErrorCode) -> None:
    fake = FakePlannerProvider()
    clock = FakeClock()
    app = build_offline_container(planner_provider=fake, clock=clock)
    guard = app.budget_guard
    budget = _planner_budget(fake)
    if case == "analysis_scope":
        budget = budget.model_copy(update={"scope_id": "analysis"})
        guard.ensure_started("analysis", "analysis")
    else:
        guard.ensure_started("audit-q", "qa")
    if case in {"provider", "model_id", "job_id"}:
        budget = budget.model_copy(update={case: "wrong"})
    reservation = guard.reserve_provider_attempt(budget)
    request = _planner_request().model_copy(update={"attempt_id": reservation.attempt_id})
    if case == "question_id":
        request = request.model_copy(update={"question_id": "wrong"})
    elif case == "cancelled":
        guard.cancel("audit-q")
    elif case == "expired":
        clock.advance(181.0)

    with pytest.raises(BudgetError) as rejected:
        _run_coro(app.planner_provider.decide(request))
    assert rejected.value.code is code
    assert fake.calls == []
    assert guard.get_ledger(budget.scope_id).total_attempts == 0
    guard.settle_not_sent(reservation, error_code=code)
    assert guard.get_account(budget.scope_id).reserved.provider_attempts == 0


def test_composed_planner_retries_count_text_only_sends_and_preserve_unknown_cost() -> None:
    clock = FakeClock()
    fake = FakePlannerProvider(scenarios=["timeout", "rate_limit", "normal"])
    app = build_offline_container(clock=clock, planner_provider=fake)
    guard = app.budget_guard
    limits = BudgetLimits(
        scope_kind="qa",
        max_provider_attempts=3,
        max_images_per_request=2,
        max_submitted_image_parts=2,
        deadline_seconds=20,
    )
    guard.ensure_started("audit-q", "qa", limits=limits)
    tool_id = guard.record_tool_execution("audit-q", tool_name="inspect_clip")
    budget = _planner_budget(fake).model_copy(
        update={"parent_tool_call_id": tool_id, "image_count": 2}
    )
    for index in range(3):
        reservation = guard.reserve_provider_attempt(budget)
        bound = _planner_request().model_copy(update={"attempt_id": reservation.attempt_id})
        if index == 0:
            clock.advance(15.0)
        if index == 2:
            bound = bound.model_copy(update={"timeout_seconds": 2.0})
            response = _run_coro(app.planner_provider.decide(bound))
            assert response.action == "final_answer"
            guard.settle_success(
                reservation,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                provider_request_id=response.provider_request_id,
                confirmed_cost_usd=0.01,
            )
        else:
            code = ErrorCode.PROVIDER_TIMEOUT if index == 0 else ErrorCode.RATE_LIMITED
            with pytest.raises(ProviderError) as failure:
                _run_coro(app.planner_provider.decide(bound))
            assert failure.value.code is code
            assert guard.get_ledger("audit-q").total_attempts == index + 1
            with pytest.raises(BudgetError):
                _run_coro(app.planner_provider.decide(bound))
            with pytest.raises(BudgetError):
                guard.settle_not_sent(reservation, error_code=code)
            with pytest.raises(BudgetError):
                guard.settle_success(reservation, input_tokens=0, output_tokens=0, images_sent=1)
            if index == 0:
                guard.settle_unknown(reservation, error_code=code)
            else:
                guard.settle_failed(reservation, error_code=code, confirmed_cost_usd=0.0)
        with pytest.raises(BudgetError):
            _run_coro(app.planner_provider.decide(bound))
        assert len(fake.calls) == index + 1
        assert fake.calls[-1].attempt_id == reservation.attempt_id
        assert fake.calls[-1].timeout_seconds == (2.0 if index == 2 else 5.0)

    ledger = guard.get_ledger("audit-q")
    assert (ledger.total_attempts, ledger.total_images_sent, ledger.reserved_attempts) == (3, 0, 0)
    assert [r.status for r in ledger.records] == ["unknown", "failed", "succeeded"]
    assert [r.attempt_id for r in ledger.records] == [call.attempt_id for call in fake.calls]
    assert len({r.attempt_id for r in ledger.records}) == 3
    assert all(r.parent_tool_call_id == tool_id and r.images_sent == 0 for r in ledger.records)
    assert ledger.cost_status == "unknown"
    assert ledger.records[0].confirmed_cost_usd is None
    assert ledger.records[0].estimated_cost_usd == pytest.approx(0.03)
    assert ledger.records[-1].provider_request_id == response.provider_request_id
    account = guard.get_account("audit-q")
    assert (account.consumed.provider_attempts, account.consumed.submitted_image_parts) == (3, 0)
    with pytest.raises(BudgetError):
        guard.reserve_provider_attempt(budget)


def test_concurrent_planner_dispatch_is_single_use_and_visible_to_a_second_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryBudgetStore()
    monkeypatch.setattr("video_agent.bootstrap.InMemoryBudgetStore", lambda: store)
    fake = FakePlannerProvider()
    app = build_offline_container(planner_provider=fake)
    guard = app.budget_guard
    guard.ensure_started("audit-q", "qa")
    reservation = guard.reserve_provider_attempt(_planner_budget(fake))
    bound = _planner_request().model_copy(update={"attempt_id": reservation.attempt_id})
    reader = BudgetGuard(store=store, clock=app.clock, config=app.config, prices=app.prices)
    assert reader.get_ledger("audit-q").total_attempts == 0
    barrier = threading.Barrier(2)

    def send() -> str:
        barrier.wait(timeout=5)
        try:
            _run_coro(app.planner_provider.decide(bound))
        except BudgetError as exc:
            return exc.code.value
        return "sent"

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(send) for _ in range(2)]
        assert sorted(call.result(timeout=5) for call in calls) == ["BUDGET_EXCEEDED", "sent"]
    assert len(fake.calls) == 1
    ledger = reader.get_ledger("audit-q")
    assert (ledger.total_attempts, ledger.total_images_sent) == (1, 0)
    assert [r.status for r in ledger.records] == ["pending"]
    guard.settle_success(reservation, input_tokens=1, output_tokens=1)
    assert reader.get_ledger("audit-q").total_attempts == 1


def test_one_reservation_cannot_dispatch_both_planner_and_vision_for_the_same_model() -> None:
    planner = FakePlannerProvider()
    vision = FakeVisionProvider()
    vision.model_id = planner.model_id
    app = build_offline_container(planner_provider=planner, vision_provider=vision)
    guard = app.budget_guard
    guard.ensure_started("audit-q", "qa")
    budget = _planner_budget(planner).model_copy(update={"image_count": 1})
    image = ImageInput(evidence_id="ev", artifact_id="art", width=2, height=2, actual_pts_ms=0)
    for planner_first in (True, False):
        reservation = guard.reserve_provider_attempt(budget)
        request = _planner_request().model_copy(update={"attempt_id": reservation.attempt_id})
        frames = VisionRequest(
            asset_id=request.asset_id,
            analysis_id=request.analysis_id,
            job_id=request.job_id,
            images=[image],
            prompt_text="Observe",
            prompt_bundle_hash="h",
            attempt_id=reservation.attempt_id,
        )
        if planner_first:
            _run_coro(app.planner_provider.decide(request))
            with pytest.raises(BudgetError):
                _run_coro(app.vision_provider.observe(frames))
            assert len(vision.calls) == 0
        else:
            _run_coro(app.vision_provider.observe(frames))
            with pytest.raises(BudgetError):
                _run_coro(app.planner_provider.decide(request))
        guard.settle_success(reservation, input_tokens=1, output_tokens=1)
    assert len(planner.calls) == len(vision.calls) == 1
    ledger = guard.get_ledger("audit-q")
    assert (ledger.total_attempts, ledger.total_images_sent) == (2, 1)
