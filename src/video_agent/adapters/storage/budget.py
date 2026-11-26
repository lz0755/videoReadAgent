"""Disk budget ledger over the M1.4 schema."""

from pathlib import Path
from typing import Literal

from video_agent.domain.models import BudgetAccount, BudgetLimits, UsageRecord


class SQLiteBudgetStore:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def get_or_create_account(
        self, scope_id: str, scope_kind: Literal["analysis", "qa"], limits: BudgetLimits
    ) -> BudgetAccount:
        raise NotImplementedError

    def save_account(self, account: BudgetAccount) -> None:
        raise NotImplementedError

    def record_usage(self, record: UsageRecord, *, dispatched: bool = False) -> None:
        raise NotImplementedError

    def list_usage(self, scope_id: str) -> list[UsageRecord]:
        raise NotImplementedError

    def list_usage_with_dispatch(self, scope_id: str) -> list[tuple[UsageRecord, bool]]:
        raise NotImplementedError
