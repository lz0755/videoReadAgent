import json
from contextlib import closing
from pathlib import Path

import pytest

from tests.contract.test_vision_provider import candidate, inputs_for
from tests.integration.test_budget_ledger import ledger_db
from tests.integration.test_observation_pipeline import provenance, register_frame
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.observations import SQLiteObservations
from video_agent.application.observations import fuse_window
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import CropBox

__all__ = ["ledger_db"]


def test_crop_keeps_its_parent_even_without_a_hint(ledger_db: Path) -> None:
    register_frame(ledger_db)
    inputs = inputs_for()
    parent = inputs.frames[0]
    box = CropBox(region_id="r1c1", x=0, y=0, width=10, height=10)
    crop = parent.model_copy(update={"crop": box, "evidence_id": "roi", "artifact_id": "crop"})
    with closing(connect_sqlite(ledger_db)) as conn:
        conn.execute(
            "INSERT INTO artifacts VALUES('crop','asset','crop','roi.jpg',?,1,'now')", ("a" * 64,)
        )
    result = fuse_window(
        inputs.model_copy(update={"frames": [crop]}),
        [candidate().model_copy(update={"evidence_ids": ["roi"]})],
        provenance(),
        originals=[parent],
    )
    SQLiteObservations(ledger_db).save(result)
    with closing(connect_sqlite(ledger_db)) as conn:
        raw = json.loads(
            conn.execute("SELECT payload FROM evidence WHERE evidence_id='roi'").fetchone()[0]
        )
    assert raw.get("parent_evidence_id") == parent.evidence_id


def test_ambiguous_region_hint_cannot_select_parent_by_list_order() -> None:
    inputs = inputs_for()
    other = inputs.frames[0].model_copy(update={"evidence_id": "other"})
    with pytest.raises(VideoAgentError):
        fuse_window(
            inputs.model_copy(update={"frames": [*inputs.frames, other]}),
            [
                candidate().model_copy(
                    update={"region_hint": "r1c1", "evidence_ids": ["frame", "other"]}
                )
            ],
            provenance(),
        )
