"""Read the published M3 evidence index for one bound analysis."""

from pathlib import Path

from video_agent.domain.models import TimeRangeMs
from video_agent.domain.ports import SearchHit


class SQLiteSearch:
    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def search(
        self,
        asset_id: str,
        analysis_id: str,
        query: str,
        *,
        limit: int = 5,
        time_range: TimeRangeMs | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError
