from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration.test_roi_evidence import scene
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.deepseek import DeepSeekVisionProvider
from video_agent.adapters.media.roi import CatalogImages
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.domain.errors import ProviderError
from video_agent.domain.models import FrameEvidence
from video_agent.domain.ports import VisionRequest

__all__ = ["scene"]


def test_raw_json_error_retains_the_actual_completion_receipt(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, files, frame = scene
    image = (
        CatalogImages(db, files)
        .prepare_frame(frame, "overview", "full", verified_high_detail=False)
        .image
    )
    port = DeepSeekVisionProvider(
        artifact_store=files, model_id="deepseek-flash", cloud_upload_enabled=True
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    stub = SimpleNamespace(
        id="receipt-actual",
        choices=[SimpleNamespace(message=SimpleNamespace(content="invalid json"))],
        usage=SimpleNamespace(prompt_tokens=987, completion_tokens=14),
    )
    monkeypatch.setattr("video_agent.adapters.llm.deepseek._complete", AsyncMock(return_value=stub))
    with pytest.raises(ProviderError) as err:
        _run_coro(
            port.observe(
                VisionRequest(
                    asset_id="asset",
                    analysis_id="a",
                    job_id="j",
                    images=[image],
                    prompt_text="read",
                    prompt_bundle_hash="bundle",
                )
            )
        )
    details = err.value.details
    assert (
        details.get("provider_request_id"),
        details.get("input_tokens"),
        details.get("output_tokens"),
    ) == ("receipt-actual", 987, 14)
