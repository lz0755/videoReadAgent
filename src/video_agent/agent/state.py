"""Visible, question-local state of the bounded planning loop."""

from dataclasses import dataclass, field
from enum import Enum, auto

from video_agent.application.evidence_service import ToolResult
from video_agent.domain.models import Answer, Claim, Question
from video_agent.domain.ports import ToolCallProposal


class Phase(Enum):
    DECIDE = auto()
    TOOLS = auto()
    FINAL = auto()


@dataclass
class PlannerState:
    question: Question
    job_id: str
    evidence: ToolResult
    phase: Phase = Phase.DECIDE
    calls: list[ToolCallProposal] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)

    def absorb(self, result: ToolResult) -> None:
        self.evidence = ToolResult(
            hits=list({h.doc_id: h for h in [*self.evidence.hits, *result.hits]}.values()),
            observations=list(
                {
                    o.observation_id: o for o in [*self.evidence.observations, *result.observations]
                }.values()
            ),
            frames=list(
                {f.evidence_id: f for f in [*self.evidence.frames, *result.frames]}.values()
            ),
            gaps=[*self.evidence.gaps, *result.gaps],
        )

    def stop(self, reason: str) -> Answer:
        return Answer(
            answer_id=f"ans_{self.question.question_id}",
            asset_id=self.question.asset_id,
            analysis_id=self.question.analysis_id,
            question_id=self.question.question_id,
            status="partial" if self.claims else "not_answerable",
            claims=self.claims,
            gaps=[reason],
            trace_id=self.job_id,
        )
