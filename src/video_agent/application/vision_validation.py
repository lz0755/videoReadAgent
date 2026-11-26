"""Validate candidate observations against exactly one server-bound input manifest."""

from video_agent.domain.models import FrameEvidence, Observation, StrictModel, TimeRangeMs, TranscriptSegment


class WindowInputs(StrictModel):
    asset_id: str
    analysis_id: str
    window: TimeRangeMs
    frames: list[FrameEvidence]
    transcripts: list[TranscriptSegment]


def validate_window(inputs: WindowInputs, candidates: list[Observation]) -> None:
    raise NotImplementedError
