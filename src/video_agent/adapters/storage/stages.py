"""Committed windows and their verified, owned artifacts."""

from pathlib import Path

from video_agent.application.observations import WindowResult
from video_agent.domain.ports import ArtifactStore


class StageCache:
    def __init__(self, db_path: Path, artifact_store: ArtifactStore) -> None:
        raise NotImplementedError

    def load(self, asset_id: str, analysis_id: str, item: str, key: str) -> WindowResult | None:
        raise NotImplementedError

    def commit(self, item: str, key: str, result: WindowResult) -> None:
        raise NotImplementedError
