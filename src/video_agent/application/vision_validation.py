"""Validate candidate observations against exactly one server-bound input manifest."""

from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import (
    FrameEvidence,
    Observation,
    StrictModel,
    TimeRangeMs,
    TranscriptSegment,
)


class WindowInputs(StrictModel):
    asset_id: str
    analysis_id: str
    window: TimeRangeMs
    frames: list[FrameEvidence]
    transcripts: list[TranscriptSegment]


def validate_window(inputs: WindowInputs, candidates: list[Observation]) -> None:
    frames = {f.evidence_id: f for f in inputs.frames}
    speech = {s.evidence_id: s for s in inputs.transcripts}
    span = inputs.window
    if len(set(frames) | set(speech)) != len(inputs.frames) + len(inputs.transcripts):
        raise DomainValidationError(ErrorCode.INVALID_CITATION, "duplicate input evidence")
    for frame in frames.values():
        if (
            frame.asset_id != inputs.asset_id
            or not span.start_ms <= frame.actual_pts_ms < span.end_ms
        ):
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "foreign or out-of-window frame"
            )
    for segment in speech.values():
        t = segment.time_range
        if (
            segment.asset_id != inputs.asset_id
            or t.start_ms >= span.end_ms
            or t.end_ms <= span.start_ms
        ):
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "foreign or non-overlapping ASR"
            )
    for observation in candidates:
        if observation.asset_id != inputs.asset_id or observation.analysis_id != inputs.analysis_id:
            raise DomainValidationError(
                ErrorCode.CROSS_ASSET_REFERENCE, "observation owner differs"
            )
        t = observation.time_range
        if not span.start_ms <= t.start_ms < t.end_ms <= span.end_ms:
            raise DomainValidationError(
                ErrorCode.INVALID_TIME_RANGE, "observation exceeds input window"
            )
        refs = set(observation.evidence_ids)
        if not refs or not refs <= (frames.keys() | speech.keys()):
            raise DomainValidationError(ErrorCode.INVALID_CITATION, "unlisted input evidence")
        if observation.kind == "observed" and not refs <= frames.keys():
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "visual fact requires frame sources"
            )
        if observation.kind == "heard" and not refs <= speech.keys():
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "heard fact requires ASR sources"
            )
        if observation.region_hint and not refs & frames.keys():
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "region hint requires a parent frame"
            )
        for ref in refs:
            if ref in frames:
                valid = t.start_ms <= frames[ref].actual_pts_ms < t.end_ms
            else:
                source = speech[ref].time_range
                valid = t.start_ms < source.end_ms and source.start_ms < t.end_ms
            if not valid:
                raise DomainValidationError(
                    ErrorCode.INVALID_TIMESTAMP, "claim misses cited evidence"
                )
