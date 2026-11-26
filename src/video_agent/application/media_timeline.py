"""Bind trusted player measurements to a particular registered source."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import PlaybackTimelineMap, PlaybackVerificationRecord, VideoAsset

_MAX_OFFSET_ERROR_MS: int = PlaybackVerificationRecord.model_fields["tolerance_ms"].default


@dataclass(frozen=True, slots=True)
class PlaybackVerification:
    asset_id: str
    artifact_id: str
    source_sha256: str
    player_id: str
    records: tuple[PlaybackVerificationRecord, ...]


def build_playback_map(
    asset: VideoAsset,
    *,
    player_id: str | None = None,
    verification: PlaybackVerification | None = None,
) -> PlaybackTimelineMap:
    """Use measurements made for this source in the requested player; otherwise offer frames."""
    if not asset.source_artifact_id:
        raise MediaError(ErrorCode.CORRUPT_MEDIA, "playback requires a registered source artifact")
    preview = PlaybackTimelineMap(
        asset_id=asset.asset_id, artifact_id=asset.source_artifact_id, status="preview_only"
    )
    if (
        asset.status != "ready"
        or verification is None
        or not player_id
        or verification.player_id != player_id
        or verification.asset_id != asset.asset_id
        or verification.artifact_id != asset.source_artifact_id
        or verification.source_sha256 != asset.sha256
        or len(verification.records) != 3
    ):
        return preview
    anchors = {"start": 0, "mid": asset.duration_ms // 2, "end": asset.duration_ms - 1}
    for rec in verification.records:
        distances = {name: abs(rec.source_pts_ms - ms) for name, ms in anchors.items()}
        if (
            not 0 <= rec.source_pts_ms < asset.duration_ms
            or rec.proxy_pts_ms < 0
            or distances.get(rec.position) != min(distances.values())
            or distances[rec.position] > rec.tolerance_ms
            or rec.observed_offset_ms != rec.proxy_pts_ms - rec.source_pts_ms
        ):
            return preview
    ordered = sorted(verification.records, key=lambda record: record.source_pts_ms)
    if any(a.proxy_pts_ms >= b.proxy_pts_ms for a, b in pairwise(ordered)):
        return preview
    # Each comparison constrains an interval of possible constant offsets.
    intervals = [
        (
            r.observed_offset_ms - min(r.tolerance_ms, _MAX_OFFSET_ERROR_MS),
            r.observed_offset_ms + min(r.tolerance_ms, _MAX_OFFSET_ERROR_MS),
        )
        for r in verification.records
    ]
    lower = max(start for start, _ in intervals)
    upper = min(end for _, end in intervals)
    if lower > upper:
        return preview
    median = sorted(r.observed_offset_ms for r in verification.records)[1]
    try:
        return PlaybackTimelineMap(
            asset_id=asset.asset_id,
            artifact_id=asset.source_artifact_id,
            status="validated_offset",
            offset_ms=min(max(median, lower), upper),
            verification_records=list(verification.records),
        )
    except ValueError:
        return preview
