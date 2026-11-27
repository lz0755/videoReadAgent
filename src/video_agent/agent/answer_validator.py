"""Validate final and interim citations against the bound question and visible evidence."""

from collections.abc import Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import ToolResult
from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import (
    Answer,
    FrameEvidence,
    Question,
    SupportKind,
    TranscriptSegment,
)


@dataclass
class AnswerValidator:
    question: Question
    duration_ms: int
    resolve: Callable[[str], FrameEvidence | TranscriptSegment | None]

    def validate(self, answer: Answer, context: ToolResult) -> None:
        q = self.question
        if (answer.asset_id, answer.analysis_id, answer.question_id) != (
            q.asset_id,
            q.analysis_id,
            q.question_id,
        ):
            raise DomainValidationError(ErrorCode.CROSS_ASSET_REFERENCE, "answer owner differs")
        support: dict[str, set[SupportKind]] = {}
        for observation in context.observations:
            if (observation.asset_id, observation.analysis_id) != (q.asset_id, q.analysis_id):
                raise DomainValidationError(ErrorCode.CROSS_ASSET_REFERENCE, "foreign observation")
            for evidence_id in observation.evidence_ids:
                support.setdefault(evidence_id, set()).add(observation.kind)
        for claim in answer.claims:
            if not claim.text.strip():
                raise DomainValidationError(ErrorCode.SUPPORTED_CLAIM_REQUIRED, "empty claim")
            for evidence_id in claim.evidence_ids:
                kinds = support.get(evidence_id, set())
                if not kinds or (
                    claim.support_kind != "inferred" and claim.support_kind not in kinds
                ):
                    raise DomainValidationError(
                        ErrorCode.INVALID_CITATION, "unlisted or unsupported source"
                    )
                source = self._source(evidence_id)
                if (
                    claim.support_kind == "heard" and not isinstance(source, TranscriptSegment)
                ) or (claim.support_kind == "observed" and not isinstance(source, FrameEvidence)):
                    raise DomainValidationError(
                        ErrorCode.INVALID_CITATION, "source modality differs"
                    )
        event = answer.localization
        if event is None:
            if q.mode == "event_localization" and answer.status == "answered":
                raise DomainValidationError(ErrorCode.GAP_REQUIRED, "event boundaries unresolved")
            return
        if q.mode != "event_localization" or not event.refined or answer.status == "not_answerable":
            raise DomainValidationError(ErrorCode.INVALID_CITATION, "unexpected localization")
        if event.start_bracket.upper_ms > event.end_bracket.lower_ms:
            raise DomainValidationError(ErrorCode.INVALID_TIME_RANGE, "boundary order is uncertain")
        sent = {f.evidence_id: f for f in context.frames}
        for bracket in (event.start_bracket, event.end_bracket):
            lower, upper = bracket.lower_ms, bracket.upper_ms
            if (
                bracket.duration_ms != self.duration_ms
                or upper - lower > 4000
                or bracket.estimate_ms != (lower + upper) // 2
            ):
                raise DomainValidationError(
                    ErrorCode.INVALID_TIME_RANGE, "unsupported event precision"
                )
            for ref, point in (
                (bracket.before_evidence_id, lower),
                (bracket.after_evidence_id, upper),
            ):
                source = self._source(ref)
                if (
                    not isinstance(source, FrameEvidence)
                    or source.actual_pts_ms != point
                    or sent.get(ref) != source
                    or "observed" not in support.get(ref, set())
                ):
                    raise DomainValidationError(
                        ErrorCode.INVALID_CITATION, "boundary differs from inspected frame"
                    )

    def _source(self, evidence_id: str) -> FrameEvidence | TranscriptSegment:
        source = self.resolve(evidence_id)
        if (
            source is None
            or source.asset_id != self.question.asset_id
            or source.evidence_id != evidence_id
        ):
            raise DomainValidationError(
                ErrorCode.INVALID_CITATION, "source cannot be resolved in this analysis"
            )
        if isinstance(source, FrameEvidence):
            in_asset = (
                source.duration_ms == self.duration_ms and source.actual_pts_ms < self.duration_ms
            )
        else:
            in_asset = source.time_range.end_ms <= self.duration_ms
        if not in_asset:
            raise DomainValidationError(
                ErrorCode.INVALID_TIMESTAMP, "evidence outside bound timeline"
            )
        return source
