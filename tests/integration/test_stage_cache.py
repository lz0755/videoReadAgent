from contextlib import closing
from pathlib import Path

import pytest

from tests.contract.test_vision_provider import candidate, inputs_for
from tests.integration.test_observation_pipeline import provenance
from tests.integration.test_roi_evidence import scene
from video_agent.adapters.media.roi import CatalogImages
from video_agent.adapters.storage.connection import connect_sqlite, create_analysis_with_job
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.stages import StageCache
from video_agent.application.observations import WindowResult, fuse_window
from video_agent.application.stages import window_key
from video_agent.domain.models import FrameEvidence

__all__ = ["scene"]


@pytest.fixture
def stored_window(
    scene: tuple[Path, CatalogArtifactStore, FrameEvidence],
) -> tuple[Path, CatalogArtifactStore, WindowResult]:
    db, store, frame = scene
    with closing(connect_sqlite(db)) as conn:
        create_analysis_with_job(
            conn,
            asset_id="asset",
            analysis_id="a",
            job_id="j",
            config_hash="config",
            prompt_bundle_hash="bundle",
            created_at="now",
        )
    prepared = CatalogImages(db, store).prepare_frame(
        frame, "software_tutorial", "r1c1", verified_high_detail=True
    )
    inputs = inputs_for().model_copy(update={"frames": [prepared.frame]})
    obs = candidate().model_copy(update={"evidence_ids": [prepared.frame.evidence_id]})
    return db, store, fuse_window(inputs, [obs], provenance(), originals=[frame])


def test_only_committed_matching_window_can_be_reused(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult],
) -> None:
    db, store, result = stored_window
    cache = StageCache(db, store)
    store.put_bytes("asset", "export", result.model_dump_json().encode(), suffix=".json")
    assert cache.load("asset", "a", "w", "key") is None
    cache.commit("w", "key", result)
    reopened = StageCache(db, CatalogArtifactStore(db.parent))
    assert reopened.load("asset", "a", "w", "key") == result
    assert reopened.load("other", "a", "w", "key") is None
    assert reopened.load("asset", "a", "w", "changed") is None


@pytest.mark.parametrize("which", ["export", "parent", "crop"])
def test_manifest_and_each_frame_must_survive_on_disk(
    stored_window: tuple[Path, CatalogArtifactStore, WindowResult], which: str
) -> None:
    db, store, result = stored_window
    cache = StageCache(db, store)
    cache.commit("w", "key", result)
    with closing(connect_sqlite(db)) as conn:
        manifest = conn.execute("SELECT artifact_id FROM stage_results").fetchone()[0]
    aid = {
        "export": manifest,
        "parent": result.originals[0].artifact_id,
        "crop": result.inputs.frames[0].artifact_id,
    }[which]
    p = store.resolve_path(aid)
    if which == "export":
        p.unlink()
    else:
        content = p.read_bytes()
        p.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    assert cache.load("asset", "a", "w", "key") is None
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT status FROM stage_results").fetchone() == ("invalidated",)


def test_all_behavior_inputs_participate_in_window_identity() -> None:
    def key(
        content: str = "content",
        model: str = "model",
        rev: str = "revision",
        prompt: str = "prompt",
        schema: str = "schema",
        sample: str = "sampling",
        crop: str = "full",
        digest: str = "inputs",
    ) -> str:
        return window_key(
            content_sha256=content,
            model=model,
            revision=rev,
            prompt_bundle=prompt,
            schema=schema,
            sampling={"profile": sample},
            crop=crop,
            input_digest=digest,
        )

    base = key()
    assert all(
        v != base
        for v in (
            key(content="2"),
            key(model="2"),
            key(rev="2"),
            key(prompt="2"),
            key(schema="2"),
            key(sample="2"),
            key(crop="2"),
            key(digest="2"),
        )
    )
