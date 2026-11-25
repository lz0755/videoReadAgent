"""Composition root for wiring concrete adapters to domain ports (M1.3).

Concrete implementations are assembled exclusively here.
No import-time network access, API key lookups, or model weight loading.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.adapters.storage.memory_budget import InMemoryBudgetStore
from video_agent.application.budget import (
    BudgetedPlannerProvider,
    BudgetedVisionProvider,
    BudgetGuard,
)
from video_agent.domain.config import AppConfig, PriceConfig
from video_agent.domain.ports import Clock, PlannerProvider, Transcriber, VisionProvider


class SystemClock:
    """Production Clock backed by UTC datetime and monotonic timer."""

    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Injected port container assembled exclusively in bootstrap.py."""

    config: AppConfig
    prices: PriceConfig
    clock: Clock
    budget_guard: BudgetGuard
    vision_provider: VisionProvider
    planner_provider: PlannerProvider
    transcriber: Transcriber


def load_app_config(path: Path | None = None) -> AppConfig:
    """Load and strictly validate config/defaults.yaml."""
    target = path if path is not None else Path("config/defaults.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


def load_price_config(path: Path | None = None) -> PriceConfig:
    """Load and strictly validate config/prices.yaml."""
    target = path if path is not None else Path("config/prices.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return PriceConfig.model_validate(raw)


def build_offline_container(
    *,
    config_path: Path | None = None,
    price_config_path: Path | None = None,
    vision_provider: VisionProvider | None = None,
    planner_provider: PlannerProvider | None = None,
    transcriber: Transcriber | None = None,
    clock: Clock | None = None,
) -> AppContainer:
    """Wire an offline container using Fake adapters without network or API keys."""
    config = load_app_config(config_path)
    resolved_price_path = (
        price_config_path
        if price_config_path is not None
        else Path(config.billing.price_config_path)
    )
    prices = load_price_config(resolved_price_path)
    clock = clock if clock is not None else SystemClock()
    guard = BudgetGuard(store=InMemoryBudgetStore(), clock=clock, config=config, prices=prices)
    vision = vision_provider if vision_provider is not None else FakeVisionProvider()
    planner = planner_provider if planner_provider is not None else FakePlannerProvider()
    return AppContainer(
        config=config,
        prices=prices,
        clock=clock,
        budget_guard=guard,
        vision_provider=BudgetedVisionProvider(vision, guard),
        planner_provider=BudgetedPlannerProvider(planner, guard),
        transcriber=transcriber if transcriber is not None else FakeTranscriber(),
    )
