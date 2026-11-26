import json
from contextlib import closing
from pathlib import Path

import pytest

from tests.contract.test_vision_provider import candidate, inputs_for
from tests.integration.test_budget_ledger import ledger_db
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.observations import SQLiteObservations
from video_agent.application.observations import WindowProvenance, fuse_window
from video_agent.domain.errors import VideoAgentError

__all__ = ["ledger_db"]


def provenance() -> WindowProvenance:
    return WindowProvenance(
        provider="fake",
        model_id="vision",
        model_revision="rev",
        prompt_bundle_hash="bundle",
        output_schema="1.1",
        sampling="5000ms",
        profile="software_tutorial",
        crop="full",
        input_digest="digest",
    )


def register_frame(db: Path) -> None:
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "INSERT INTO artifacts VALUES('image','asset','frame','frame.jpg',?,1,'now')",
            ("9" * 64,),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fusion_keeps_both_readings_and_original_asr(ledger_db: Path) -> None:
    register_frame(ledger_db)
    first = candidate().model_copy(
        update={"text": "diameter 20 mm", "provenance": {"model_id": "untrusted"}}
    )
    second = first.model_copy(update={"text": "diameter 26 mm"})
    window = fuse_window(inputs_for(), [first, second], provenance())
    store = SQLiteObservations(ledger_db)
    store.save(window)
    store.save(window)
    with closing(connect_sqlite(ledger_db)) as conn:
        rows = conn.execute("SELECT text,kind,provenance FROM observations").fetchall()
        assert {(r[0], r[1]) for r in rows} == {
            ("diameter 20 mm", "observed"),
            ("diameter 26 mm", "observed"),
            ("heard words", "heard"),
        }
        assert all(json.loads(r[2])["input_digest"] == "digest" for r in rows)
        assert all(json.loads(r[2])["model_id"] == "vision" for r in rows)
        assert conn.execute("SELECT COUNT(*) FROM observation_evidence").fetchone() == (3,)
        raw = dict(conn.execute("SELECT evidence_id,payload FROM evidence"))
        assert len(raw) == 2 and json.loads(raw["frame"])["requested_ms"] == 10
        assert json.loads(raw["frame"])["actual_pts_ms"] == 13
        assert json.loads(raw["speech"])["text"] == "heard words"


def test_changed_raw_evidence_cannot_overwrite_source_or_publish_new_claims(
    ledger_db: Path,
) -> None:
    register_frame(ledger_db)
    inputs = inputs_for()
    store = SQLiteObservations(ledger_db)
    store.save(fuse_window(inputs, [candidate()], provenance()))
    forged = inputs.model_copy(
        update={"transcripts": [inputs.transcripts[0].model_copy(update={"text": "other"})]}
    )
    with pytest.raises(VideoAgentError):
        store.save(
            fuse_window(forged, [candidate().model_copy(update={"text": "new"})], provenance())
        )
    with closing(connect_sqlite(ledger_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (2,)
        assert (
            json.loads(
                conn.execute("SELECT payload FROM evidence WHERE kind='transcript'").fetchone()[0]
            )["text"]
            == "heard words"
        )
