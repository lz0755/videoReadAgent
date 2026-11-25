"""Contract tests for M1.3 domain Protocols, DTOs, and bootstrap DI."""

from __future__ import annotations

import inspect
from datetime import UTC
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.bootstrap import SystemClock, build_offline_container
from video_agent.domain.models import AudioTimelineMap, RationalTime
from video_agent.domain.ports import (
    ArtifactStore,
    BudgetStore,
    Clock,
    EvidenceRepository,
    ImageInput,
    MediaBackend,
    PlannerProvider,
    PlannerRequest,
    SearchPort,
    Transcriber,
    TranscribeRequest,
    VideoProvider,
    VisionProvider,
    VisionRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ports_declare_async_boundaries_and_runtime_protocols() -> None:
    """Async provider and ASR ports expose coroutine methods; Fakes conform to Protocols."""
    for proto in (
        Clock,
        MediaBackend,
        Transcriber,
        VisionProvider,
        PlannerProvider,
        VideoProvider,
        ArtifactStore,
        EvidenceRepository,
        SearchPort,
        BudgetStore,
    ):
        assert getattr(proto, "_is_protocol", False) is True

    vision = FakeVisionProvider()
    planner = FakePlannerProvider()
    transcriber = FakeTranscriber()

    assert isinstance(vision, VisionProvider)
    assert isinstance(planner, PlannerProvider)
    assert isinstance(transcriber, Transcriber)
    assert isinstance(SystemClock(), Clock)

    assert inspect.iscoroutinefunction(vision.observe)
    assert inspect.iscoroutinefunction(planner.decide)
    assert inspect.iscoroutinefunction(transcriber.transcribe)


def test_request_dtos_enforce_strict_domain_contracts() -> None:
    """Port DTOs forbid unknown fields, empty image batches, and invalid timeouts/VAD."""
    with pytest.raises(ValidationError):
        VisionRequest(
            asset_id="asset_1",
            analysis_id="analysis_1",
            job_id="job_1",
            prompt_text="Observe frames",
            prompt_bundle_hash="h" * 64,
            images=[],
            timeout_seconds=10.0,
        )

    with pytest.raises(ValidationError):
        VisionRequest(
            asset_id="asset_1",
            analysis_id="analysis_1",
            job_id="job_1",
            prompt_text="Observe frames",
            prompt_bundle_hash="h" * 64,
            images=[
                ImageInput(
                    evidence_id="ev_1",
                    artifact_id="art_1",
                    width=1920,
                    height=1080,
                    actual_pts_ms=0,
                )
            ],
            timeout_seconds=0.0,
        )

    with pytest.raises(ValidationError):
        PlannerRequest(
            asset_id="asset_1",
            run_id="analysis_1",  # type: ignore[call-arg]
            question_id="q_1",
            job_id="job_1",
            prompt_text="Decide",
            prompt_bundle_hash="h" * 64,
            available_evidence_ids=["ev_1"],
        )

    with pytest.raises(ValidationError):
        TranscribeRequest(
            asset_id="asset_1",
            audio_artifact_id="art_audio_1",
            audio_timeline=AudioTimelineMap(
                origin_pts=0,
                origin_time_base=RationalTime(numerator=1, denominator=1000),
                audio_offset_ms=200,
                extracted_start_ms=0,
            ),
            vad_filter=True,  # type: ignore[arg-type]
        )


def test_bootstrap_wires_offline_container_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap.build_offline_container wires Fakes and validated configs offline."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    container = build_offline_container(
        config_path=REPO_ROOT / "config" / "defaults.yaml",
        price_config_path=REPO_ROOT / "config" / "prices.yaml",
    )
    assert isinstance(container.vision_provider, VisionProvider)
    assert isinstance(container.planner_provider, PlannerProvider)
    assert isinstance(container.transcriber, Transcriber)
    assert isinstance(container.clock, Clock)
    assert container.config.privacy.cloud_upload_enabled is False
    assert container.prices.currency == "USD"

    now = container.clock.now_utc()
    assert now.tzinfo == UTC
    t1 = container.clock.monotonic()
    t2 = container.clock.monotonic()
    assert t2 >= t1
