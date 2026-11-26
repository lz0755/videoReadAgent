"""Composition root for wiring concrete adapters to domain ports (M1.3 / M1.5.2).

Concrete implementations are assembled exclusively here.
No import-time network access, API key lookups, or model weight loading.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import yaml

from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.asr.faster_whisper import FasterWhisperTranscriber
from video_agent.adapters.llm.deepseek import (
    DeepSeekPlannerProvider,
    DeepSeekVisionProvider,
    slice_endpoint,
)
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.adapters.llm.openai_slice import OPENAI_MODEL_ID
from video_agent.adapters.media.probe import probe_managed
from video_agent.adapters.media.thin_media import LocalArtifactStore, PyAVMediaBackend
from video_agent.adapters.storage.filesystem import (
    CatalogArtifactStore,
    ManagedArtifactFiles,
    ManagedSourceStore,
    checked_local_path,
)
from video_agent.adapters.storage.media_cache import FileMediaCache, model_directory_digest
from video_agent.adapters.storage.memory_budget import InMemoryBudgetStore
from video_agent.application.budget import (
    BudgetedPlannerProvider,
    BudgetedVisionProvider,
    BudgetGuard,
    SliceRunLimits,
)
from video_agent.application.ingest import AssetImporter
from video_agent.application.media_cache import CachedMediaBackend, CachedTranscriber
from video_agent.application.media_timeline import build_playback_map
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.application.slice import VerticalSliceResult
from video_agent.domain.config import AppConfig, PriceConfig, ProviderConfig
from video_agent.domain.errors import BudgetError, ErrorCode, MediaError, ProviderError
from video_agent.domain.models import VideoAsset
from video_agent.domain.ports import (
    Clock,
    MediaBackend,
    PlannerProvider,
    Transcriber,
    VisionProvider,
)


def build_asset_importer(
    data_root: Path,
    config: AppConfig,
    *,
    quota_bytes: int | None = None,
    progress: Callable[[int], None] | None = None,
) -> AssetImporter:
    store = ManagedSourceStore(
        data_root,
        max_bytes=config.media.max_file_bytes,
        chunk_bytes=config.ingest.copy_chunk_bytes,
        quota_bytes=quota_bytes,
        progress=progress,
    )
    return AssetImporter(store, probe_managed, config.media.max_duration_ms)


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


@dataclass(frozen=True, slots=True)
class SliceContainer:
    """Injected vertical-slice container assembled exclusively in bootstrap.py."""

    mode: Literal["offline_fake", "live"]
    config: AppConfig
    prices: PriceConfig
    clock: Clock
    artifact_store: LocalArtifactStore
    media_backend: MediaBackend
    prompt_registry: PromptRegistry
    budget_guard: BudgetGuard
    vision_provider: VisionProvider
    planner_provider: PlannerProvider
    transcriber: Transcriber
    release_asr: Callable[[], None] | None = None
    registered_artifacts: CatalogArtifactStore | None = None

    def close(self) -> None:
        if self.release_asr is not None:
            self.release_asr()

    def save_evidence_manifest(self, result: VerticalSliceResult) -> str:
        """Publish one reviewable local artifact and return its path relative to data_root."""
        store = self.registered_artifacts or self.artifact_store
        root = store.root_dir.parent
        artifact_ids = [
            result.asset.source_artifact_id,
            result.audio_artifact_id,
            *(frame.artifact_id for frame in result.frames),
        ]
        trace = {
            "schema_version": "1.1",
            "provider_mode": result.provider_mode,
            "asset_id": result.asset.asset_id,
            "sha256": result.asset.sha256,
            "analysis_id": result.analysis_id,
            "prompt_bundle_hash": result.prompt_bundle_hash,
            "source_artifact_id": result.asset.source_artifact_id,
            "playback_timeline_map": build_playback_map(result.asset).model_dump(),
            "audio_artifact_id": result.audio_artifact_id,
            "audio_timeline": result.audio_timeline.model_dump() if result.audio_timeline else None,
            "artifact_paths": {
                aid: store.resolve_path(aid).relative_to(root).as_posix()
                for aid in artifact_ids
                if aid is not None
            },
            "frames": [f.model_dump() for f in result.frames],
            "transcript_segments": [s.model_dump() for s in result.transcript_segments],
            "observations": [o.model_dump() for o in result.observations],
            "questions": [
                {
                    "question_id": qr.question.question_id,
                    "query_text": qr.question.query_text,
                    "search_hits": [hit.model_dump() for hit in qr.search_hits],
                    "answer": qr.answer.model_dump(),
                }
                for qr in result.question_results
            ],
            "question_errors": {
                qid: {
                    "error_code": failure.error_code,
                    "query_text": failure.question.query_text,
                    "search_hits": [hit.model_dump() for hit in failure.search_hits],
                }
                for qid, failure in result.question_errors.items()
            },
        }
        artifact_id = store.put_bytes(
            result.asset.asset_id,
            "slice_evidence",
            json.dumps(trace, ensure_ascii=False, indent=2).encode("utf-8"),
            suffix=".json",
        )
        return store.resolve_path(artifact_id).relative_to(root).as_posix()


def _load_slice_approval(path: Path) -> tuple[bool, SliceRunLimits | None]:
    if not path.is_file():
        raise ValueError("slice budget approval file was not found")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("slice budget approval must be a mapping")
    if "authorization" in raw:
        auth = raw.pop("authorization")
        if not isinstance(auth, dict) or {"live_enabled", "cloud_egress_authorized"} & raw.keys():
            raise ValueError("slice approval requires one authorization mapping")
    else:
        auth = {key: raw.pop(key, False) for key in ("live_enabled", "cloud_egress_authorized")}
    flags = [auth.get(key, False) for key in ("live_enabled", "cloud_egress_authorized")]
    if not all(isinstance(flag, bool) for flag in flags):
        raise ValueError("slice authorization flags must be YAML booleans")
    if "budget" in raw:
        limits = raw.pop("budget")
        if raw:
            raise ValueError("put all numeric slice limits in the budget mapping")
    else:
        limits = raw
    if not isinstance(limits, dict) or any(v is None for v in limits.values()):
        raise ValueError("slice limits must be a mapping with numeric values")
    return all(flags), SliceRunLimits.model_validate(limits) if limits else None


def _ffmpeg_version() -> str:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            close_fds=True,
            timeout=5,
            check=True,
        )
        return proc.stdout.splitlines()[0].decode("utf-8", "replace")
    except OSError, subprocess.SubprocessError, IndexError:
        return "unavailable"


def build_slice_container(
    *,
    mode: Literal["offline", "live"],
    data_root: Path,
    live_provider: Literal["deepseek", "openai"] | None = None,
    repo_root: Path = Path("."),
    config_path: Path | None = None,
    price_config_path: Path | None = None,
    prompts_registry_path: Path | None = None,
    cloud_egress_authorized: bool = False,
    budget_config_path: Path | None = None,
    asr_model_path: Path | None = None,
    vision_provider: VisionProvider | None = None,
    planner_provider: PlannerProvider | None = None,
    transcriber: Transcriber | None = None,
    clock: Clock | None = None,
) -> SliceContainer:
    """Wire a real-media M1.5 vertical slice container in offline-fake or explicit live mode."""
    config = load_app_config(config_path or repo_root / "config" / "defaults.yaml")
    if live_provider is not None:
        if mode != "live":
            raise ValueError("explicit provider selection requires live mode")
        model, key_env = (
            (OPENAI_MODEL_ID, "OPENAI_API_KEY")
            if live_provider == "openai"
            else ("deepseek-flash", "DEEPSEEK_API_KEY")
        )
        config = config.model_copy(
            update={
                "provider": ProviderConfig(name=live_provider, model=model, api_key_env=key_env)
            }
        )
    if mode == "live" and config.provider.name == "openai":
        price_path = Path(config.billing.price_config_path).with_name("prices_openai.yaml")
        config = config.model_copy(
            update={
                "billing": config.billing.model_copy(update={"price_config_path": str(price_path)})
            }
        )
    resolved_price_path = (
        price_config_path
        if price_config_path is not None
        else repo_root / config.billing.price_config_path
    )
    resolved_prompts_path = (
        prompts_registry_path
        if prompts_registry_path is not None
        else repo_root / config.prompts.registry_path
    )
    prices = load_price_config(resolved_price_path)
    prompt_registry = PromptRegistry(resolved_prompts_path)
    resolved_clock = clock if clock is not None else SystemClock()
    run_limits = None
    if budget_config_path is not None:
        cloud_egress_authorized, run_limits = _load_slice_approval(budget_config_path)
    guard = BudgetGuard(
        store=InMemoryBudgetStore(),
        clock=resolved_clock,
        config=config,
        prices=prices,
        run_limits=run_limits,
    )
    artifact_store = CatalogArtifactStore(data_root)
    provider_files = ManagedArtifactFiles(data_root)
    source_store = ManagedSourceStore(
        data_root,
        max_bytes=config.media.max_file_bytes,
        chunk_bytes=config.ingest.copy_chunk_bytes,
        quota_bytes=None,
        progress=None,
    )
    importer = AssetImporter(source_store, probe_managed, config.media.max_duration_ms)

    def register(path: str, asset_id: str) -> VideoAsset:
        selected = Path(path)
        existing = source_store.existing(
            selected, asset_id, max_duration_ms=config.media.max_duration_ms
        )
        if existing is not None:
            return existing
        return importer.import_file(
            selected, asset_id=asset_id, authorization="local slice user selection"
        )

    cache = FileMediaCache(checked_local_path(data_root / "media-cache"), artifact_store)
    media_backend = CachedMediaBackend(
        PyAVMediaBackend(artifact_store, register=register),
        cache,
        frame_profile={
            "processor": "pts-v2",
            "stream": "v:0",
            "rotation": "display-matrix",
            "crop": "full",
            "jpeg_quality": 92,
            "max_long_edge_px": None,
            "pyav": version("av"),
            "pillow": version("pillow"),
        },
        audio_profile={
            "processor": "canonical-pcm-v2",
            "stream": "a:0",
            "rate": 16000,
            "channels": 1,
            "format": "s16le",
            "ffmpeg": _ffmpeg_version(),
            "pyav": version("av"),
        },
    )
    release_asr = None

    if mode == "live":
        if not cloud_egress_authorized:
            raise ProviderError(
                ErrorCode.CLOUD_DISABLED,
                "live vertical slice requires explicit cloud_egress_authorized=True",
            )
        api_key_env = config.provider.api_key_env
        slice_endpoint(config.provider.name, config.provider.model, api_key_env)
        if not os.environ.get(api_key_env, "").strip():
            raise ProviderError(
                ErrorCode.CREDENTIAL_MISSING,
                f"live vertical slice requires '{api_key_env}' environment variable",
            )
        if run_limits is None:
            raise BudgetError(
                ErrorCode.BUDGET_EXCEEDED, "live slice requires an approved numeric budget"
            )
        resolved_vision: VisionProvider = (
            vision_provider
            if vision_provider is not None
            else DeepSeekVisionProvider(
                artifact_store=provider_files,
                model_id=config.provider.model,
                api_key_env=api_key_env,
                cloud_upload_enabled=True,
                provider_name=config.provider.name,
            )
        )
        resolved_planner: PlannerProvider = (
            planner_provider
            if planner_provider is not None
            else DeepSeekPlannerProvider(
                model_id=config.provider.model,
                api_key_env=api_key_env,
                cloud_upload_enabled=True,
                provider_name=config.provider.name,
            )
        )
        resolved_transcriber: Transcriber
        if transcriber is None:
            supervised = FasterWhisperTranscriber(
                artifact_store,
                model_path=asr_model_path,
                duration_for_asset=source_store.duration_for_asset,
            )
            previous_digest: str | None = None

            def asr_profile() -> dict[str, object]:
                nonlocal previous_digest
                if asr_model_path is None:
                    raise MediaError(ErrorCode.DEPENDENCY_MISSING, "local ASR weights are required")
                digest = model_directory_digest(asr_model_path)
                if previous_digest is not None and previous_digest != digest:
                    supervised.unload_model()
                previous_digest = digest
                return {
                    "backend": "faster-whisper",
                    "version": version("faster-whisper"),
                    "runtime": version("ctranslate2"),
                    "weights": digest,
                    "device": "cpu",
                    "compute_type": "int8",
                    "adapter_version": "faster-whisper-v2",
                    "word_timestamps": True,
                    "max_initial_timestamp": 30.0,
                    "mapping": "canonical-pcm-v2",
                }

            resolved_transcriber = CachedTranscriber(supervised, cache, profile=asr_profile)
            release_asr = supervised.close
        else:
            resolved_transcriber = transcriber
        resolved_mode: Literal["offline_fake", "live"] = "live"
    else:
        resolved_vision = (
            vision_provider
            if vision_provider is not None
            else FakeVisionProvider(duration_ms=config.media.max_duration_ms)
        )
        resolved_planner = (
            planner_provider if planner_provider is not None else FakePlannerProvider()
        )
        resolved_transcriber = (
            transcriber
            if transcriber is not None
            else CachedTranscriber(
                FakeTranscriber(),
                cache,
                profile=lambda: {"backend": "offline_fake", "version": "fake-asr-v1"},
            )
        )
        resolved_mode = "offline_fake"

    return SliceContainer(
        mode=resolved_mode,
        config=config,
        prices=prices,
        clock=resolved_clock,
        artifact_store=provider_files,
        media_backend=media_backend,
        prompt_registry=prompt_registry,
        budget_guard=guard,
        vision_provider=BudgetedVisionProvider(resolved_vision, guard),
        planner_provider=BudgetedPlannerProvider(resolved_planner, guard),
        transcriber=resolved_transcriber,
        release_asr=release_asr,
        registered_artifacts=artifact_store,
    )
