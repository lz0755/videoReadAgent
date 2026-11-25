"""Thread-safe in-memory BudgetStore implementing domain.ports.BudgetStore (M1.7)."""

from __future__ import annotations

import threading
from typing import Literal

from video_agent.domain.models import BudgetAccount, BudgetLimits, UsageRecord


class InMemoryBudgetStore:
    """In-memory BudgetStore with identical semantics to future M3.0 SQLite BudgetStore."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, BudgetAccount] = {}
        self._usage: dict[str, dict[str, tuple[UsageRecord, bool]]] = {}

    def get_or_create_account(
        self,
        scope_id: str,
        scope_kind: Literal["analysis", "qa"],
        limits: BudgetLimits,
    ) -> BudgetAccount:
        with self._lock:
            existing = self._accounts.get(scope_id)
            if existing is not None:
                return existing
            account = BudgetAccount(
                scope_id=scope_id,
                scope_kind=scope_kind,
                limits=limits,
            )
            self._accounts[scope_id] = account
            return account

    def save_account(self, account: BudgetAccount) -> None:
        with self._lock:
            self._accounts[account.scope_id] = account

    def record_usage(self, record: UsageRecord, *, dispatched: bool = False) -> None:
        with self._lock:
            by_attempt = self._usage.setdefault(record.scope_id, {})
            previous = by_attempt.get(record.attempt_id)
            was_dispatched = dispatched or (previous is not None and previous[1])
            by_attempt[record.attempt_id] = (record, was_dispatched)

    def list_usage(self, scope_id: str) -> list[UsageRecord]:
        return [record for record, _ in self.list_usage_with_dispatch(scope_id)]

    def list_usage_with_dispatch(self, scope_id: str) -> list[tuple[UsageRecord, bool]]:
        with self._lock:
            return list(self._usage.get(scope_id, {}).values())
