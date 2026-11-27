from typing import Literal

import pytest

from video_agent.domain.models import PlaybackTimelineMap, PlaybackVerificationRecord
from video_agent.ui.playback import playback_seconds


def measured(offset: int) -> PlaybackTimelineMap:
    positions: list[Literal["start", "mid", "end"]] = ["start", "mid", "end"]
    return PlaybackTimelineMap(
        asset_id="asset",
        artifact_id="source",
        status="validated_offset",
        offset_ms=offset,
        verification_records=[
            PlaybackVerificationRecord(
                position=where, source_pts_ms=t, proxy_pts_ms=t + offset, observed_offset_ms=offset
            )
            for t, where in zip([0, 1500, 2960], positions, strict=True)
        ],
    )


def test_seek_requires_matching_source_measurements_and_browser_authentication() -> None:
    mapping = measured(5000)

    def seek(asset: str, artifact: str, secure: bool) -> float | None:
        return playback_seconds(
            mapping,
            asset_id=asset,
            artifact_id=artifact,
            actual_ms=1040,
            authenticated_source=secure,
        )

    assert seek("asset", "source", True) == 6.04
    assert seek("another", "source", True) is None and seek("asset", "other", True) is None
    assert seek("asset", "source", False) is None
    preview = PlaybackTimelineMap(asset_id="asset", artifact_id="source", status="preview_only")
    assert (
        playback_seconds(
            preview,
            asset_id="asset",
            artifact_id="source",
            actual_ms=1040,
            authenticated_source=True,
        )
        is None
    )


@pytest.mark.parametrize("point", [200, 1200, 2200])
def test_audio_evidence_stays_on_its_original_axis(point: int) -> None:
    assert (
        playback_seconds(
            measured(0),
            asset_id="asset",
            artifact_id="source",
            actual_ms=point,
            authenticated_source=True,
        )
        == point / 1000
    )
