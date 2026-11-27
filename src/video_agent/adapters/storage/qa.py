"""Durable reads and job publication for a single bound question."""

from pathlib import Path

from video_agent.domain.models import (
    Answer,
    FrameEvidence,
    Observation,
    Question,
    RegionId,
    TimeRangeMs,
    TranscriptSegment,
)
from video_agent.domain.ports import SearchHit


class SQLiteQA:
    def __init__(self, db: Path, question: Question) -> None:
        self.db = db
        self.question = question

    def resolve(self, evidence_id: str) -> FrameEvidence | TranscriptSegment | None:
        raise NotImplementedError

    def read_observation(self, hit: SearchHit) -> Observation:
        raise NotImplementedError

    def allow_region(self, region: RegionId, window: TimeRangeMs) -> bool:
        raise NotImplementedError

    def context(self, hit: SearchHit) -> list[FrameEvidence]:
        raise NotImplementedError

    def publish(self, answer: Answer, job_id: str) -> None:
        raise NotImplementedError
