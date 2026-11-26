import hashlib
import io
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from tests.contract.test_vision_provider import inputs_for
from tests.integration.test_sqlite_contract import _seed_ready_asset
from video_agent.adapters.llm.vision import check_images
from video_agent.adapters.media.roi import CatalogImages
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import FrameEvidence, RegionId
from video_agent.domain.ports import VisionRequest


@pytest.fixture
def scene(tmp_path: Path) -> tuple[Path, CatalogArtifactStore, FrameEvidence]:
    db = tmp_path / "agent.sqlite3"
    run_migrations(db, data_root=tmp_path, writers_stopped=True)
    with closing(connect_sqlite(db)) as conn:
        _seed_ready_asset(conn, "asset", "source")
        _seed_ready_asset(conn, "other", "source2")
    store = CatalogArtifactStore(tmp_path)
    picture = Image.new("RGB", (1920, 1080), (40, 70, 90))
    picture.paste((201, 12, 20), (0, 0, 640, 360))
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    art = store.put_bytes("asset", "frame", stream.getvalue(), suffix=".png")
    with Image.open(io.BytesIO(store.read_bytes(art))) as check:
        assert check.size == (1920, 1080) and check.getpixel((300, 150)) == (201, 12, 20)
    return (
        db,
        store,
        inputs_for().frames[0].model_copy(update={"artifact_id": art}),
    )


def test_original_cropped_geometry_and_provider_dimensions(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence],
) -> None:
    db, store, frame = scene
    images = CatalogImages(db, store)
    full = images.prepare_frame(frame, "software_tutorial", "full", verified_high_detail=True)
    assert full.frame == frame and full.image.artifact_id == frame.artifact_id
    assert (full.image.width, full.image.height, full.image.detail) == (1920, 1080, "high")
    assert full.sha256 == hashlib.sha256(store.read_bytes(frame.artifact_id)).hexdigest()
    cropped = images.prepare_frame(frame, "software_tutorial", "r1c1", verified_high_detail=True)
    box = cropped.frame.crop
    assert box and (box.x, box.y, box.width, box.height) == (0, 0, 704, 396)
    assert cropped.parent == frame and cropped.frame.evidence_id != frame.evidence_id
    assert (cropped.frame.raw_pts, cropped.frame.actual_pts_ms) == (
        frame.raw_pts,
        frame.actual_pts_ms,
    )
    with Image.open(io.BytesIO(store.read_bytes(cropped.frame.artifact_id))) as pixel:
        assert pixel.size == (704, 396)
        r, g, b = cast(tuple[int, int, int], pixel.getpixel((300, 150)))
        assert r > 185 and max(g, b) < 35
    overview = images.prepare_frame(frame, "overview", "full", verified_high_detail=False)
    assert (overview.image.width, overview.image.height) == (
        960,
        540,
    ) and overview.image.detail != "high"


def test_untrusted_region_asset_grid_and_dimensions_rejected(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence],
) -> None:
    db, store, frame = scene
    images = CatalogImages(db, store)
    with pytest.raises((ValueError, VideoAgentError)):
        images.prepare_frame(
            frame, "overview", cast(RegionId, "0,1,2,3"), verified_high_detail=False
        )
    for bad in [
        frame.model_copy(update={"asset_id": "other"}),
        frame.model_copy(
            update={
                "artifact_id": store.put_bytes(
                    "asset", "grid_preview", store.read_bytes(frame.artifact_id), suffix=".png"
                )
            }
        ),
    ]:
        with pytest.raises(VideoAgentError):
            images.prepare_frame(bad, "software_tutorial", "full", verified_high_detail=True)
    full = images.prepare_frame(frame, "software_tutorial", "full", verified_high_detail=True)
    request = VisionRequest(
        asset_id="asset",
        analysis_id="a",
        job_id="j",
        prompt_text="read",
        prompt_bundle_hash="hash",
        images=[full.image],
    )
    check_images(images, request, max_images=8)
    for parts in [[full.image] * 9, [full.image.model_copy(update={"width": 1})]]:
        with pytest.raises(VideoAgentError):
            check_images(images, request.model_copy(update={"images": parts}), max_images=8)
    huge = io.BytesIO()
    Image.new("RGB", (8193, 2)).save(huge, format="PNG")
    aid = store.put_bytes("asset", "frame", huge.getvalue(), suffix=".png")
    with Image.open(io.BytesIO(store.read_bytes(aid))) as pixels:
        assert pixels.size == (8193, 2)
    large = full.image.model_copy(
        update={"artifact_id": aid, "width": 8193, "height": 2, "mime_type": "image/png"}
    )
    with pytest.raises(VideoAgentError):
        check_images(images, request.model_copy(update={"images": [large]}), max_images=8)
