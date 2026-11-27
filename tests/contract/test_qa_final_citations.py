from unittest.mock import Mock

import pytest

from tests.contract.test_answer_evidence import proposed, question
from tests.contract.test_vision_provider import candidate, inputs_for
from tests.unit.agent.test_controller import context, harness
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.fake import FakePlannerProvider
from video_agent.agent.answer_validator import AnswerValidator
from video_agent.application.evidence_service import ToolResult
from video_agent.domain.errors import DomainValidationError, ErrorCode, VideoAgentError
from video_agent.domain.models import BoundaryBracket, EventLocalization, TimeRangeMs
from video_agent.domain.ports import PlannerRequest, PlannerResponse, ToolCallProposal


def test_only_one_active_frame_cannot_support_both_claimed_event_boundaries() -> None:
    frames = [
        inputs_for()
        .frames[0]
        .model_copy(
            update={"evidence_id": str(t), "requested_ms": t, "actual_pts_ms": t, "raw_pts": t}
        )
        for t in (10, 20, 30)
    ]
    refs = {f.evidence_id: f for f in frames}
    validator = AnswerValidator(
        question().model_copy(update={"mode": "event_localization"}), 100, refs.get
    )
    evidence = ToolResult(
        frames=frames,
        observations=[
            candidate().model_copy(
                update={
                    "observation_id": f.evidence_id,
                    "evidence_ids": [f.evidence_id],
                    "time_range": TimeRangeMs(start_ms=f.actual_pts_ms, end_ms=f.actual_pts_ms + 1),
                }
            )
            for f in frames
        ],
    )

    def bracket(first: int, last: int) -> BoundaryBracket:
        return BoundaryBracket(
            lower_ms=first,
            upper_ms=last,
            duration_ms=100,
            before_evidence_id=str(first),
            after_evidence_id=str(last),
            estimate_ms=(first + last) // 2,
        )

    answer = proposed().model_copy(
        update={
            "claims": [
                proposed().claims[0].model_copy(update={"evidence_ids": ["10", "20", "30"]})
            ],
            "localization": EventLocalization(
                label="visible event",
                refined=True,
                start_bracket=bracket(10, 20),
                end_bracket=bracket(20, 30),
            ),
        }
    )
    with pytest.raises(VideoAgentError):
        validator.validate(answer, evidence)


def test_fallback_revalidates_retained_claim_after_its_evidence_becomes_unavailable() -> None:
    class Interim(FakePlannerProvider):
        async def decide(self, req: PlannerRequest) -> PlannerResponse:
            initial = await super().decide(req)
            return initial.model_copy(
                update={
                    "action": "call_tools",
                    "tool_calls": [
                        ToolCallProposal(
                            tool_call_id="refresh",
                            tool_name="search_evidence",
                            arguments={"query": "next"},
                        )
                    ],
                }
            )

    planner = Interim()
    frame = (
        inputs_for("A", "X")
        .frames[0]
        .model_copy(update={"requested_ms": 15, "actual_pts_ms": 15, "raw_pts": 15})
    )
    repository = {"frame": frame}
    ctrl, guard, _, _ = harness(planner)
    ctrl.validate = AnswerValidator(ctrl.executor.service.question, 100, repository.get).validate
    search = ctrl.executor.service.search
    assert isinstance(search, Mock)

    def unavailable(*args: object, **kwargs: object) -> None:
        repository.clear()
        raise DomainValidationError(ErrorCode.INVALID_CITATION, "record unavailable")

    search.search.side_effect = unavailable
    answer = _run_coro(ctrl.run("J", context()))
    assert answer.status == "not_answerable" and answer.claims == [] and answer.gaps
    assert len(planner.calls) == 1 and guard.get_ledger("Q").total_attempts == 1
