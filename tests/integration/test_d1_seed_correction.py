from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from eval.d1 import verify_packet
from tests.integration.test_d1_freeze import packet_fixture

__all__ = ["packet_fixture"]


@pytest.mark.parametrize("affected", [True, False])
def test_only_an_excessive_frozen_endpoint_can_be_corrected_by_evaluator(
    packet_fixture: tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
    affected: bool,
) -> None:
    root, videos, original, template = packet_fixture
    seed, packet = deepcopy(original), deepcopy(template)
    q = seed[0]
    note = {
        "question_id": q["question_id"],
        "ref_index": 0,
        "original_end_ms": 3001 if affected else 2000,
        "end_ms": 3000 if affected else 1900,
        "reason": "reviewed video stream end",
    }
    q["evidence_refs"][0]["end_ms"] = note["original_end_ms"]
    packet["seed_evidence_corrections"] = [note]
    if affected:
        verify_packet(videos, seed, packet, evidence_root=root, repo_root=root)
    else:
        with pytest.raises(ValueError):
            verify_packet(videos, seed, packet, evidence_root=root, repo_root=root)
    assert q["evidence_refs"][0]["end_ms"] == note["original_end_ms"]
