"""Locate an event from a coarse index hit and two bounded visual inspections."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import ToolResult
from video_agent.domain.errors import DomainValidationError, ErrorCode, VideoAgentError
from video_agent.domain.models import (
    Answer,
    BoundaryBracket,
    Claim,
    EventLocalization,
    FrameEvidence,
    Question,
    TimeRangeMs,
)
from video_agent.domain.ports import SearchHit, ToolCallProposal


def _bracket(
    result: ToolResult, window: TimeRangeMs, question: Question, duration: int, *, start: bool
) -> BoundaryBracket | None:
    frames = sorted(result.frames, key=lambda f: f.actual_pts_ms)
    states: dict[str, set[str]] = {f.evidence_id: set() for f in frames}
    for obs in result.observations:
        if (
            (obs.asset_id, obs.analysis_id) != (question.asset_id, question.analysis_id)
            or obs.kind != "observed"
            or len(obs.evidence_ids) != 1
        ):
            continue
        state = obs.text.strip().casefold()
        eid = obs.evidence_ids[0]
        if eid in states and state in {"active", "inactive", "unclear"}:
            states[eid].add(state)

    ordered: list[tuple[FrameEvidence, str]] = []
    for frame in frames:
        if (
            frame.asset_id != question.asset_id
            or frame.duration_ms != duration
            or frame.crop
            or not window.start_ms <= frame.actual_pts_ms < window.end_ms
            or (ordered and ordered[-1][0].actual_pts_ms == frame.actual_pts_ms)
        ):
            return None
        labels = states[frame.evidence_id]
        if len(labels) > 1:
            return None
        ordered.append((frame, next(iter(labels), "unclear")))

    before, after = ("inactive", "active") if start else ("active", "inactive")
    transitions = []
    for (left, a), (right, b) in zip(ordered, ordered[1:], strict=False):
        if (a, b) == (after, before):
            return None
        if (a, b) == (before, after):
            transitions.append((left, right))
    if len(transitions) != 1:
        return None
    left, right = transitions[0]
    lo, hi = left.actual_pts_ms, right.actual_pts_ms
    if hi - lo > 4000:
        return None
    return BoundaryBracket(
        lower_ms=lo,
        upper_ms=hi,
        before_evidence_id=left.evidence_id,
        after_evidence_id=right.evidence_id,
        estimate_ms=(lo + hi) // 2,
        duration_ms=duration,
    )


@dataclass
class EventLocalizer:
    question: Question
    duration_ms: int
    context: Callable[[SearchHit], list[FrameEvidence]]
    execute: Callable[[ToolCallProposal], Awaitable[ToolResult]]

    async def run(self, job_id: str, initial: ToolResult) -> tuple[Answer, ToolResult]:
        question = self.question
        claims: list[Claim] = []
        brackets: list[BoundaryBracket] = []
        gaps: list[str] = []
        exposed = initial
        localization = None
        try:
            windows = self._windows(initial)
            for side, window in zip(("start", "end"), windows, strict=True):
                result = await self.execute(
                    ToolCallProposal(
                        tool_call_id=f"{question.question_id}-{side}",
                        tool_name="inspect_clip",
                        arguments={**window.model_dump(), "region_id": "full"},
                    )
                )
                exposed = ToolResult(
                    hits=initial.hits,
                    observations=[*exposed.observations, *result.observations],
                    frames=[*exposed.frames, *result.frames],
                    gaps=[*exposed.gaps, *result.gaps],
                )
                bracket = _bracket(
                    result, window, question, self.duration_ms, start=side == "start"
                )
                edge = "开始" if side == "start" else "结束"
                if bracket is None:
                    gaps.append(f"{edge}边界缺少可区分的前后帧，或出现相互矛盾的状态。")
                    continue
                brackets.append(bracket)
                claims.append(
                    Claim(
                        text=f"{edge}边界位于 {bracket.lower_ms} 至 {bracket.upper_ms} 毫秒之间。",
                        support_kind="inferred",
                        evidence_ids=[bracket.before_evidence_id, bracket.after_evidence_id],
                    )
                )
            if len(brackets) == 2:
                start, end = brackets
                if start.upper_ms < end.lower_ms:
                    localization = EventLocalization(
                        label=question.query_text,
                        start_bracket=start,
                        end_bracket=end,
                        refined=True,
                    )
                else:
                    gaps.append("两个边界区间无法证实事件的先后顺序。")
                    claims.clear()
        except VideoAgentError as exc:
            # Keep the first supported side if the second exceeds its budget or deadline.
            gaps.append(
                f"{exc.code.value}: {exc.message}"
                if isinstance(exc, DomainValidationError)
                else exc.code.value
            )
        return Answer(
            answer_id=f"ans_{question.question_id}",
            asset_id=question.asset_id,
            analysis_id=question.analysis_id,
            question_id=question.question_id,
            trace_id=job_id,
            status="answered" if localization else "partial" if claims else "not_answerable",
            claims=claims,
            gaps=gaps,
            localization=localization,
        ), exposed

    def _windows(self, initial: ToolResult) -> tuple[TimeRangeMs, TimeRangeMs]:
        scope = (self.question.asset_id, self.question.analysis_id)
        observed = {
            o.observation_id: o
            for o in initial.observations
            if o.kind == "observed" and (o.asset_id, o.analysis_id) == scope
        }
        for hit in initial.hits:
            obs = observed.get(hit.source_id)
            if (
                hit.source_kind != "observation"
                or (hit.asset_id, hit.analysis_id) != scope
                or obs is None
                or set(obs.evidence_ids) != set(hit.evidence_ids)
            ):
                continue
            frames = self.context(hit)
            if any(f.asset_id != scope[0] or f.duration_ms != self.duration_ms for f in frames):
                raise DomainValidationError(ErrorCode.CROSS_ASSET_REFERENCE, "粗候选帧归属不符。")
            cited = {
                f.evidence_id: f.actual_pts_ms for f in frames if f.evidence_id in hit.evidence_ids
            }
            if set(cited) != set(hit.evidence_ids):
                continue
            first, last = min(cited.values()), max(cited.values())
            if not hit.time_range.start_ms <= first <= last < hit.time_range.end_ms:
                continue
            before = max((f.actual_pts_ms for f in frames if f.actual_pts_ms < first), default=None)
            after = min((f.actual_pts_ms for f in frames if f.actual_pts_ms > last), default=None)
            if before is None or after is None:
                continue
            start = TimeRangeMs(start_ms=before, end_ms=first + 1)
            end = TimeRangeMs(start_ms=last, end_ms=after + 1)
            if all(w.end_ms - w.start_ms <= 8000 for w in (start, end)):
                return start, end
        raise DomainValidationError(
            ErrorCode.NO_FRAME_IN_RANGE, "当前粗检索无法用两个八秒回看窗口覆盖事件两侧。"
        )
