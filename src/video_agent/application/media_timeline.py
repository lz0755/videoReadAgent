"""Bind trusted player measurements to a particular registered source."""

from __future__ import annotations

from dataclasses import dataclass

from video_agent.domain.models import PlaybackTimelineMap, PlaybackVerificationRecord, VideoAsset


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
    raise NotImplementedError
