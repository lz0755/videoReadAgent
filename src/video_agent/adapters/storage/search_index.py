"""Transactional search_docs, FTS and index stage publication."""

from pathlib import Path

from video_agent.application.indexing import DOCUMENT_VERSION
from video_agent.application.stages import WindowStages


class SQLiteSearchIndex:
    def __init__(self, db_path: Path, stages: WindowStages) -> None:
        raise NotImplementedError

    def publish(self, asset_id: str, analysis_id: str, windows: dict[str,str], *,
                document_version: str = DOCUMENT_VERSION, partial: bool = False) -> int:
        raise NotImplementedError
