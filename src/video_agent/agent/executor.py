"""Execute one validated proposal against one question's budget."""

from dataclasses import dataclass

from video_agent.application.budget import BudgetGuard
from video_agent.application.evidence_service import EvidenceService, ToolResult
from video_agent.domain.config import QaConfig
from video_agent.domain.ports import ToolCallProposal


@dataclass
class ToolExecutor:
    guard: BudgetGuard
    service: EvidenceService
    config: QaConfig

    async def execute(self, proposal: ToolCallProposal) -> ToolResult:
        raise NotImplementedError
