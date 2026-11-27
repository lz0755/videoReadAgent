"""Execute one validated proposal against one question's budget."""

from dataclasses import dataclass

from video_agent.agent.tools import validate_tool
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
        request = validate_tool(proposal.tool_name, proposal.arguments, self.service, self.config)
        parent = self.guard.record_tool_execution(
            self.service.question.question_id,
            tool_name=proposal.tool_name,
            expensive=proposal.tool_name in ("inspect_clip", "compare_frames"),
        )
        return await self.service.run(request, parent)
