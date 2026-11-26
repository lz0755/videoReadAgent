from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from eval.d1 import verify_packet
from tests.integration.test_d1_freeze import packet_fixture

__all__ = ["packet_fixture"]


@pytest.mark.parametrize(
    "fault", ["relabel_id", "denied_license", "invented_duration", "invented_size"]
)
def test_freeze_rejects_changed_source_facts_and_duplicate_events(
    packet_fixture: tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
    fault: str,
) -> None:
    root, source, seed, original = packet_fixture
    videos, packet = deepcopy(source), deepcopy(original)
    if fault == "relabel_id":
        first, second = packet["events"][:2]
        assert first["video_id"] == second["video_id"] and first["event_id"] != second["event_id"]
        second.update(first | {"event_id": second["event_id"]})
    elif fault == "denied_license":
        videos[-1]["authorization"] = {"license_name": "none", "usage_scope": "not_authorized"}
    elif fault == "invented_duration":
        videos[-1]["ffprobe"]["duration_ms"] = 512000
        packet["questions"][-2]["evidence_refs"][0]["end_ms"] = 511000
    else:
        videos[-1]["ffprobe"].update(width=48, height=720)
    with pytest.raises(ValueError):
        verify_packet(videos, seed, packet, evidence_root=root, repo_root=root)
