from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration.test_roi_evidence import scene
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.deepseek import DeepSeekVisionProvider
from video_agent.adapters.llm.vision import StrictVisionProvider
from video_agent.adapters.media.roi import CatalogImages
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.domain.errors import ErrorCode, ProviderError, VideoAgentError
from video_agent.domain.models import FrameEvidence
from video_agent.domain.ports import VisionRequest

__all__ = ["scene"]


def test_wire_shape_checked_before_normalized_candidate_is_accepted(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, store, frame = scene
    prepared = CatalogImages(db, store).prepare_frame(
        frame, "software_tutorial", "full", verified_high_detail=True
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"observations":[{"text":{"invalid":42},"evidence_ids":["frame"],"roi":[0,0,1,1]}]}'
                )
            )
        ],
        model="deepseek-flash",
        id="receipt",
        usage=None,
    )
    transport = AsyncMock(return_value=completion)
    monkeypatch.setattr("video_agent.adapters.llm.deepseek._complete", transport)
    actual = DeepSeekVisionProvider(
        artifact_store=store, model_id="deepseek-flash", cloud_upload_enabled=True
    )
    request = VisionRequest(
        asset_id="asset",
        analysis_id="a",
        job_id="j",
        images=[prepared.image],
        prompt_text="read",
        prompt_bundle_hash="bundle",
    )
    with pytest.raises(ProviderError) as err:
        _run_coro(StrictVisionProvider(actual).observe(request))
    assert err.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED and transport.await_count == 1


def test_catalog_crop_cannot_be_used_as_uncropped_parent(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence],
) -> None:
    db, store, frame = scene
    images = CatalogImages(db, store)
    cropped = images.prepare_frame(frame, "software_tutorial", "r1c1", verified_high_detail=True)
    with pytest.raises(VideoAgentError):
        images.prepare_frame(
            frame.model_copy(update={"artifact_id": cropped.frame.artifact_id}),
            "software_tutorial",
            "r1c1",
            verified_high_detail=True,
        )
