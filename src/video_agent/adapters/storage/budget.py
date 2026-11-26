"""SQLite persistence for the application BudgetGuard. All decisions stay in the guard."""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.application.budget import SliceRunLimits
from video_agent.domain.errors import BudgetError, ErrorCode
from video_agent.domain.models import (
    BudgetAccount,
    BudgetCounters,
    BudgetLimits,
    BudgetReservation,
    UsageRecord,
)


class SQLiteBudgetStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if getattr(self._local, "connection", None) is not None:
            yield
            return
        with closing(connect_sqlite(self._path)) as conn, immediate_transaction(conn):
            conn.row_factory = sqlite3.Row
            self._local.connection = conn
            try:
                yield
            finally:
                del self._local.connection

    @property
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection = self._local.connection
        return conn

    def _job(self, scope_id: str, kind: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM jobs WHERE kind=? AND "
            "(CASE kind WHEN 'qa' THEN question_id ELSE analysis_id END)=?",
            (kind, scope_id),
        ).fetchone()
        if row is None:
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "budget scope requires a bound job")
        return row

    def bind_approval(self, scope_id: str, proposed: SliceRunLimits) -> SliceRunLimits:
        row = self._conn.execute(
            "SELECT json_extract(limits_json,'$.run_approval') "
            "FROM budget_accounts WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        if row[0] is not None:
            return SliceRunLimits.model_validate_json(row[0])
        self._conn.execute(
            "UPDATE budget_accounts SET limits_json=json_set(limits_json,"
            "'$.run_approval',json(?)) WHERE scope_id=?",
            (proposed.model_dump_json(), scope_id),
        )
        return proposed

    def observe_clock(self, scope_id: str, utc: str) -> str:
        row = self._conn.execute(
            "SELECT updated_at FROM budget_accounts WHERE scope_id=?", (scope_id,)
        ).fetchone()
        previous = row[0] if row and row[0] else utc
        if datetime.fromisoformat(utc) >= datetime.fromisoformat(previous):
            self._conn.execute(
                "UPDATE budget_accounts SET updated_at=? WHERE scope_id=?", (utc, scope_id)
            )
        return str(previous)

    def check_binding(self, scope_id: str, job_id: str, asset_id: str, analysis_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE job_id=? AND asset_id=? AND analysis_id=? AND "
            "(CASE kind WHEN 'qa' THEN question_id ELSE analysis_id END)=? AND "
            "status IN ('queued','running') AND cancel_requested=0",
            (job_id, asset_id, analysis_id, scope_id),
        ).fetchone()
        if row is None:
            raise BudgetError(
                ErrorCode.BUDGET_EXCEEDED, "outgoing request has no matching active job"
            )

    def get_or_create_account(
        self, scope_id: str, scope_kind: Literal["analysis", "qa"], limits: BudgetLimits
    ) -> BudgetAccount:
        with self.transaction():
            job = self._job(scope_id, scope_kind)
            row = self._conn.execute(
                "SELECT * FROM budget_accounts WHERE scope_id=?", (scope_id,)
            ).fetchone()
            if row is None:
                account = BudgetAccount(
                    scope_id=scope_id,
                    scope_kind=scope_kind,
                    limits=limits,
                    started_at_utc=job["started_at_utc"],
                    deadline_at_utc=job["deadline_at_utc"],
                    cancelled=bool(job["cancel_requested"]),
                )
                self.save_account(account)
                return account
            if row["scope_kind"] != scope_kind:
                raise BudgetError(ErrorCode.IDEMPOTENCY_CONFLICT, "scope kind differs")
            limits_payload = json.loads(row["limits_json"])
            limits_payload.pop("run_approval", None)
            return BudgetAccount(
                scope_id=scope_id,
                scope_kind=scope_kind,
                limits=BudgetLimits.model_validate(limits_payload),
                reserved=BudgetCounters.model_validate_json(row["reserved_json"]),
                consumed=BudgetCounters.model_validate_json(row["consumed_json"]),
                started_at_utc=row["started_at_utc"],
                deadline_at_utc=row["deadline_at_utc"],
                cancelled=bool(
                    row["cancelled"] or job["cancel_requested"] or job["status"] == "cancelled"
                ),
                has_unknown_external_result=bool(row["has_unknown_external_result"]),
            )

    def save_account(self, account: BudgetAccount) -> None:
        with self.transaction():
            job = self._job(account.scope_id, account.scope_kind)
            self._conn.execute(
                """INSERT INTO budget_accounts(scope_id,scope_kind,limits_json,reserved_json,
                consumed_json,started_at_utc,deadline_at_utc,cancelled,has_unknown_external_result,
                updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET
                reserved_json=excluded.reserved_json, consumed_json=excluded.consumed_json,
                started_at_utc=COALESCE(budget_accounts.started_at_utc,excluded.started_at_utc),
                deadline_at_utc=COALESCE(budget_accounts.deadline_at_utc,excluded.deadline_at_utc),
                cancelled=max(cancelled,excluded.cancelled),
                has_unknown_external_result=excluded.has_unknown_external_result""",
                (
                    account.scope_id,
                    account.scope_kind,
                    account.limits.model_dump_json(),
                    account.reserved.model_dump_json(),
                    account.consumed.model_dump_json(),
                    account.started_at_utc,
                    account.deadline_at_utc,
                    account.cancelled,
                    account.has_unknown_external_result,
                    account.started_at_utc or "",
                ),
            )
            self._conn.execute(
                """UPDATE jobs SET started_at_utc=COALESCE(started_at_utc,?),
                deadline_at_utc=COALESCE(deadline_at_utc,?),cancel_requested=max(cancel_requested,?)
                WHERE job_id=?""",
                (account.started_at_utc, account.deadline_at_utc, account.cancelled, job["job_id"]),
            )

    def insert_attempt(
        self,
        reservation: BudgetReservation,
        pending: UsageRecord,
        *,
        operation_key: str | None,
        explicit_retry: bool,
        previous_attempt_id: str | None,
        price_version: str,
    ) -> None:
        scope = reservation.scope_id
        row = self._conn.execute(
            "SELECT scope_kind FROM budget_accounts WHERE scope_id=?", (scope,)
        ).fetchone()
        if row is None or self._job(scope, row[0])["job_id"] != reservation.job_id:
            raise BudgetError(ErrorCode.BUDGET_EXCEEDED, "attempt requires the bound job")
        if operation_key is not None:
            prior = self._conn.execute(
                """SELECT attempt_id,status FROM usage_records WHERE scope_id=?
                AND json_extract(reservation_json,'$.operation_key')=? AND status!='not_sent'
                ORDER BY rowid DESC LIMIT 1""",
                (scope, operation_key),
            ).fetchone()
            if (
                prior
                and not explicit_retry
                and not (prior["status"] == "failed" and prior["attempt_id"] == previous_attempt_id)
            ):
                raise BudgetError(
                    ErrorCode.EXTERNAL_RESULT_UNKNOWN,
                    "external operation already recorded; explicit retry required",
                )
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO usage_records(attempt_id,job_id,scope_id,parent_tool_call_id,provider,
            model_id,status,reservation_json,usage_json,cost_status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                reservation.attempt_id,
                reservation.job_id,
                scope,
                reservation.parent_tool_call_id,
                reservation.provider,
                reservation.model_id,
                pending.status,
                json.dumps(
                    {
                        "reservation": reservation.model_dump(),
                        "operation_key": operation_key,
                        "price_version": price_version,
                        "dispatched": False,
                    }
                ),
                pending.model_dump_json(),
                pending.cost_status,
                now,
                now,
            ),
        )

    def read_attempt(self, attempt_id: str) -> tuple[BudgetReservation, UsageRecord, bool] | None:
        with self.transaction():
            row = self._conn.execute(
                "SELECT reservation_json,usage_json FROM usage_records WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return None
            meta = json.loads(row["reservation_json"])
            return (
                BudgetReservation.model_validate(meta["reservation"]),
                UsageRecord.model_validate_json(row["usage_json"]),
                bool(meta["dispatched"]),
            )

    def record_usage(self, record: UsageRecord, *, dispatched: bool = False) -> None:
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE usage_records SET usage_json=?,status=?,cost_status=?,
                provider_request_id=?,error_code=?,updated_at=?,
                reservation_json=json_set(reservation_json,'$.dispatched',
                max(json_extract(reservation_json,'$.dispatched'),?))
                WHERE attempt_id=? AND job_id=? AND scope_id=? AND status='pending'""",
                (
                    record.model_dump_json(),
                    record.status,
                    record.cost_status,
                    record.provider_request_id,
                    record.error_code,
                    datetime.now(UTC).isoformat(),
                    int(dispatched),
                    record.attempt_id,
                    record.job_id,
                    record.scope_id,
                ),
            )
            if cursor.rowcount != 1:
                raise BudgetError(ErrorCode.IDEMPOTENCY_CONFLICT, "attempt is absent or settled")

    def list_usage_with_dispatch(self, scope_id: str) -> list[tuple[UsageRecord, bool]]:
        with self.transaction():
            return [
                (UsageRecord.model_validate_json(row[0]), bool(row[1]))
                for row in self._conn.execute(
                    "SELECT usage_json,json_extract(reservation_json,'$.dispatched') "
                    "FROM usage_records WHERE scope_id=? ORDER BY rowid",
                    (scope_id,),
                )
            ]

    def list_usage(self, scope_id: str) -> list[UsageRecord]:
        return [record for record, _ in self.list_usage_with_dispatch(scope_id)]
