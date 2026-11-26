"""Durable M3.0 accounts measured at the provider boundary on independent connections."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from tests.integration.test_sqlite_contract import TS, _seed_ready_asset
from tests.unit.test_budget_guard import FakeClock, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.adapters.storage.budget import SQLiteBudgetStore
from video_agent.adapters.storage.connection import (
    connect_sqlite,
    create_analysis_with_job,
    create_question_with_job,
)
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.application.budget import BudgetGuard
from video_agent.bootstrap import load_app_config, load_price_config
from video_agent.domain.errors import BudgetError, ErrorCode, ProviderError
from video_agent.domain.models import RequestBudget
from video_agent.domain.policies import build_budget_limits, check_cloud_egress_allowed
from video_agent.domain.ports import (
    ImageInput,
    PlannerRequest,
    PlannerResponse,
    VisionRequest,
    VisionResponse,
)


@pytest.fixture
def ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.sqlite"
    run_migrations(db, data_root=tmp_path, writers_stopped=True)
    with closing(connect_sqlite(db)) as conn:
        _seed_ready_asset(conn, "asset", "source")
        create_analysis_with_job(
            conn,
            analysis_id="a",
            asset_id="asset",
            job_id="j",
            config_hash="config",
            prompt_bundle_hash="bundle",
            created_at=TS,
        )
        create_question_with_job(
            conn,
            question_id="q",
            asset_id="asset",
            analysis_id="a",
            job_id="jq",
            query_text="What?",
            mode="qa",
            created_at=TS,
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (2,)
    return db


def guard_for(db: Path, clock: FakeClock | None = None) -> BudgetGuard:
    return BudgetGuard(
        store=SQLiteBudgetStore(db),
        clock=clock or FakeClock(),
        config=load_app_config(),
        prices=load_price_config(),
    )


def vision(job_id: str = "j") -> VisionRequest:
    return VisionRequest(
        asset_id="asset",
        analysis_id="a",
        job_id=job_id,
        prompt_text="inspect frames",
        prompt_bundle_hash="bundle",
        images=[
            ImageInput(evidence_id=f"e{i}", artifact_id=f"f{i}", width=8, height=8, actual_pts_ms=i)
            for i in range(8)
        ],
    )


def planner() -> PlannerRequest:
    return PlannerRequest(
        asset_id="asset",
        analysis_id="a",
        job_id="jq",
        question_id="q",
        prompt_text="answer",
        prompt_bundle_hash="bundle",
    )


def test_independent_connections_compete_for_one_send(ledger_db: Path) -> None:
    limits = build_budget_limits(load_app_config(), "analysis").model_copy(
        update={"max_provider_attempts": 1}
    )
    fake = FakeVisionProvider()
    barrier = Barrier(2)

    def run(index: int) -> str:
        guard = guard_for(ledger_db)
        guard.ensure_started("a", "analysis", limits=limits)
        executor = ProviderExecutor(guard, authorize=lambda: None)
        barrier.wait(timeout=10)
        try:
            _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key=f"w{index}"))
        except BudgetError as exc:
            return exc.code.value
        return "sent"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ["BUDGET_EXCEEDED", "sent"]
    assert len(fake.calls) == 1
    reopened = guard_for(ledger_db)
    reopened.ensure_started("a", "analysis")
    assert reopened.get_ledger("a").total_images_sent == 8


def test_nested_retry_charges_one_tool_and_every_image(ledger_db: Path) -> None:
    guard = guard_for(ledger_db)
    guard.ensure_started("q", "qa")
    parent = guard.record_tool_execution("q", tool_name="inspect_clip", expensive=True)
    fake = FakeVisionProvider(scenarios=["rate_limit", "normal"])
    executor = ProviderExecutor(guard, authorize=lambda: None)
    _run_coro(
        executor.observe(
            fake, vision("jq"), scope_id="q", operation_key="inspect", parent_tool_call_id=parent
        )
    )
    assert [r.parent_tool_call_id for r in guard.get_ledger("q").records] == [parent, parent]
    assert len({r.attempt_id for r in fake.calls}) == 2
    counts = guard.get_account("q").consumed
    assert (
        counts.tool_calls,
        counts.expensive_tool_calls,
        counts.provider_attempts,
        counts.submitted_image_parts,
    ) == (1, 1, 2, 16)


def test_timeout_blocks_automatic_replay_and_original_deadline_survives(ledger_db: Path) -> None:
    clock = FakeClock()
    guard = guard_for(ledger_db, clock)
    started = guard.ensure_started("a", "analysis")
    fake = FakeVisionProvider(scenarios=["timeout", "normal"])
    fake.model_id = "deepseek-flash"  # use a nonzero reference price on the offline port
    executor = ProviderExecutor(guard, authorize=lambda: None)
    with pytest.raises(ProviderError):
        _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key="window"))
    assert len(fake.calls) == 1
    reopened = guard_for(ledger_db, clock)
    assert reopened.ensure_started("a", "analysis").deadline_at_utc == started.deadline_at_utc
    ledger = reopened.get_ledger("a")
    assert (ledger.cost_status, ledger.total_attempts, ledger.total_images_sent) == (
        "unknown",
        1,
        8,
    )
    assert ledger.estimated_cost_usd > 0 and ledger.records[0].confirmed_cost_usd is None
    executor = ProviderExecutor(reopened, authorize=lambda: None)
    with pytest.raises(BudgetError) as err:
        _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key="window"))
    assert err.value.code == ErrorCode.EXTERNAL_RESULT_UNKNOWN
    clock.advance(3601)
    with pytest.raises(BudgetError) as err:
        _run_coro(
            executor.observe(
                fake, vision(), scope_id="a", operation_key="window", explicit_retry=True
            )
        )
    assert err.value.code == ErrorCode.DEADLINE_EXCEEDED and len(fake.calls) == 1


def test_dispatch_commit_visible_before_provider_and_result_separate(ledger_db: Path) -> None:
    guard = guard_for(ledger_db)
    limits = build_budget_limits(load_app_config(), "qa").model_copy(
        update={"max_provider_attempts": 1}
    )
    guard.ensure_started("q", "qa", limits=limits)

    class InspectSend(FakePlannerProvider):
        async def decide(self, request: PlannerRequest) -> PlannerResponse:
            record, dispatched = SQLiteBudgetStore(ledger_db).list_usage_with_dispatch("q")[0]
            assert dispatched and record.status == "pending" and record.images_sent == 0
            with closing(connect_sqlite(ledger_db)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return await super().decide(request)

    fake = InspectSend()
    executor = ProviderExecutor(guard, authorize=lambda: None)
    with pytest.raises(BudgetError) as err:
        _run_coro(executor.decide(fake, planner(), operation_key="reason"))
    assert err.value.code == ErrorCode.BUDGET_EXCEEDED and fake.calls == []
    _run_coro(executor.decide(fake, planner(), operation_key="answer", final=True))
    reopened = guard_for(ledger_db)
    reopened.ensure_started("q", "qa")
    assert reopened.get_ledger("q").records[0].cost_status == "confirmed"
    with closing(connect_sqlite(ledger_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stage_results").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM answers").fetchone() == (0,)
        assert conn.execute("SELECT deadline_at_utc FROM jobs WHERE job_id='jq'").fetchone() == (
            reopened.get_account("q").deadline_at_utc,
        )


def test_transaction_rollback_and_settlement_replay(ledger_db: Path) -> None:
    guard = guard_for(ledger_db)
    guard.ensure_started("a", "analysis")
    request = RequestBudget(
        scope_id="a",
        job_id="j",
        provider="fake",
        model_id="fake-vision",
        image_count=8,
        estimated_cost_usd=0.2,
    )
    with closing(connect_sqlite(ledger_db)) as conn:
        conn.execute(
            "CREATE TRIGGER fail_attempt BEFORE INSERT ON usage_records "
            "BEGIN SELECT RAISE(ABORT,'fixture rejects insert'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture rejects insert"):
        guard.reserve_provider_attempt(request)
    assert guard.get_account("a").reserved.provider_attempts == 0
    with closing(connect_sqlite(ledger_db)) as conn:
        conn.execute("DROP TRIGGER fail_attempt")
    reservation = guard.reserve_provider_attempt(request)
    outgoing = vision().model_copy(update={"attempt_id": reservation.attempt_id})
    guard.prepare_vision_send(outgoing, provider="fake", model_id="fake-vision")
    guard.settle_success(reservation, input_tokens=1, output_tokens=2, confirmed_cost_usd=0.13)
    restarted = guard_for(ledger_db)
    restarted.ensure_started("a", "analysis")
    restarted.settle_success(reservation, input_tokens=1, output_tokens=2, confirmed_cost_usd=0.13)
    assert restarted.get_ledger("a").confirmed_cost_usd == 0.13
    assert restarted.get_account("a").consumed.provider_attempts == 1


@pytest.mark.parametrize(
    ("cloud", "key", "code"),
    [(False, True, ErrorCode.CLOUD_DISABLED), (True, False, ErrorCode.CREDENTIAL_MISSING)],
)
def test_denied_egress_creates_no_attempt(
    ledger_db: Path, cloud: bool, key: bool, code: ErrorCode
) -> None:
    guard = guard_for(ledger_db)
    guard.ensure_started("a", "analysis")
    fake = FakeVisionProvider()
    executor = ProviderExecutor(
        guard,
        authorize=lambda: check_cloud_egress_allowed(
            cloud_upload_enabled=cloud, has_credential=key
        ),
    )
    with pytest.raises(ProviderError) as err:
        _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key="window"))
    assert err.value.code == code and not fake.calls and not guard.get_ledger("a").records


def test_job_binding_and_persisted_cancellation(ledger_db: Path) -> None:
    guard = guard_for(ledger_db)
    guard.ensure_started("q", "qa")
    fake = FakeVisionProvider()
    executor = ProviderExecutor(guard, authorize=lambda: None)
    for changed in ({"asset_id": "other"}, {"analysis_id": "other"}, {"job_id": "j"}):
        with pytest.raises(BudgetError):
            _run_coro(
                executor.observe(
                    fake,
                    vision("jq").model_copy(update=changed),
                    scope_id="q",
                    operation_key="inspect",
                )
            )
    with closing(connect_sqlite(ledger_db)) as conn:
        conn.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id='jq'")
    with pytest.raises(BudgetError) as err:
        _run_coro(executor.observe(fake, vision("jq"), scope_id="q", operation_key="inspect"))
    assert err.value.code == ErrorCode.JOB_CANCELLED and fake.calls == []


def test_interrupted_dispatch_is_not_repeated_on_reopen(ledger_db: Path) -> None:
    class Interrupt(FakeVisionProvider):
        async def observe(self, request: VisionRequest) -> VisionResponse:
            self.calls.append(request)
            raise SystemExit(10)

    guard = guard_for(ledger_db)
    guard.ensure_started("a", "analysis")
    interrupted = Interrupt()
    interrupted.model_id = "deepseek-flash"
    with pytest.raises(SystemExit):
        _run_coro(
            ProviderExecutor(guard, authorize=lambda: None).observe(
                interrupted, vision(), scope_id="a", operation_key="first"
            )
        )
    restarted = guard_for(ledger_db)
    restarted.ensure_started("a", "analysis")
    executor = ProviderExecutor(restarted, authorize=lambda: None)
    fake = FakeVisionProvider()
    fake.model_id = "deepseek-flash"
    with pytest.raises(BudgetError) as err:
        _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key="first"))
    assert err.value.code == ErrorCode.EXTERNAL_RESULT_UNKNOWN and not fake.calls
    assert restarted.get_ledger("a").estimated_cost_usd > 0
    _run_coro(
        executor.observe(fake, vision(), scope_id="a", operation_key="first", explicit_retry=True)
    )
    assert restarted.get_ledger("a").total_attempts == 2
    with pytest.raises(BudgetError):
        _run_coro(executor.observe(fake, vision(), scope_id="a", operation_key="first"))
    assert len(fake.calls) == 1
