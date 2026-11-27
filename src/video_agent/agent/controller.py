"""Finite planning loop over an already bound QA execution."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.agent.executor import ToolExecutor
from video_agent.agent.state import Phase, PlannerState
from video_agent.agent.tools import tool_schema
from video_agent.application.evidence_service import ToolResult
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.domain.errors import BudgetError, ErrorCode, VideoAgentError
from video_agent.domain.models import Answer, BudgetAccount
from video_agent.domain.ports import PlannerRequest, PlannerResponse


def _remaining_attempts(account: BudgetAccount) -> int:
    return (
        account.limits.max_provider_attempts
        - account.consumed.provider_attempts
        - account.reserved.provider_attempts
    )


@dataclass
class Controller:
    executor: ToolExecutor
    prompts: PromptRegistry
    decide: Callable[[PlannerRequest, bool], Awaitable[PlannerResponse]]
    validate: Callable[[Answer, ToolResult], None]

    async def run(self, job_id: str, initial: ToolResult) -> Answer:
        question = self.executor.service.question
        scope = question.question_id
        state = PlannerState(question, job_id, initial)
        guard = self.executor.guard
        try:
            while True:
                remaining = guard.remaining_seconds(scope)
                if any(r.status in {"unknown", "pending"} for r in guard.get_ledger(scope).records):
                    return self._stop(state, ErrorCode.EXTERNAL_RESULT_UNKNOWN.value)
                if state.phase == Phase.TOOLS:
                    for call in state.calls:
                        if (
                            call.tool_name != "search_evidence"
                            and _remaining_attempts(guard.get_account(scope)) == 1
                        ):
                            break
                        try:
                            state.absorb(await self.executor.execute(call))
                        except BudgetError as exc:
                            # A nested retry can consume the last non-final attempt.
                            if (
                                exc.code == ErrorCode.BUDGET_EXCEEDED
                                and _remaining_attempts(guard.get_account(scope)) == 1
                            ):
                                break
                            raise
                    state.phase = Phase.DECIDE
                    continue
                account = guard.get_account(scope)
                limits, used = account.limits, account.consumed
                attempts = _remaining_attempts(account)
                rounds = (limits.max_rounds or self.executor.config.max_rounds) - used.rounds
                if min(attempts, rounds) <= 0:
                    return self._stop(state, ErrorCode.BUDGET_EXCEEDED.value)
                if min(attempts, rounds) == 1 or used.tool_calls >= (
                    limits.max_tool_calls or self.executor.config.max_tool_calls
                ):
                    state.phase = Phase.FINAL
                guard.begin_round(scope)
                req = self._request(state, account, remaining)
                decision = await self.decide(req, state.phase == Phase.FINAL)
                if decision.answer is not None:
                    self.validate(decision.answer, state.evidence)
                    if decision.answer.claims:
                        state.claims = list(decision.answer.claims)
                guard.remaining_seconds(scope)
                if (
                    decision.action == "final_answer"
                    and decision.answer is not None
                    and not decision.tool_calls
                ):
                    return decision.answer
                if (
                    state.phase == Phase.FINAL
                    or decision.action != "call_tools"
                    or not decision.tool_calls
                ):
                    return self._stop(state, "Planner did not return a usable final answer.")
                state.calls = decision.tool_calls
                state.phase = Phase.TOOLS
        except VideoAgentError as exc:
            return self._stop(state, exc.code.value)

    def _stop(self, state: PlannerState, reason: str) -> Answer:
        answer = state.stop(reason)
        retained = []
        for claim in answer.claims:
            try:
                self.validate(answer.model_copy(update={"claims": [claim]}), state.evidence)
            except VideoAgentError:
                continue
            retained.append(claim)
        if len(retained) == len(answer.claims):
            return answer
        return answer.model_copy(
            update={
                "claims": retained,
                "status": "partial" if retained else "not_answerable",
                "gaps": list(dict.fromkeys([reason, ErrorCode.INVALID_CITATION.value])),
            }
        )

    def _request(
        self, state: PlannerState, account: BudgetAccount, remaining: float
    ) -> PlannerRequest:
        question = state.question
        evidence = state.evidence
        summary = {
            "observations": [
                o.model_dump(include={"text", "kind", "evidence_ids", "time_range", "region_hint"})
                for o in evidence.observations
            ],
            "unobserved_ranges": [gap.model_dump() for gap in evidence.gaps],
        }
        variables: dict[str, object] = {
            "question_text": question.query_text,
            "evidence_summary": json.dumps(summary, ensure_ascii=False),
        }
        schema = tool_schema()
        if state.phase != Phase.FINAL:
            variables["remaining_budget"] = json.dumps(
                {
                    "limits": account.limits.model_dump(),
                    "consumed": account.consumed.model_dump(),
                    "reserved": account.reserved.model_dump(),
                    "seconds_left": remaining,
                    "tools": schema,
                    "response_shape": {
                        "call_tools": {
                            "action": "call_tools",
                            "tool_calls": [
                                {
                                    "tool_name": "<whitelisted name>",
                                    "arguments": "<object matching tool schema>",
                                }
                            ],
                            "answer": None,
                        },
                        "final_answer": {
                            "action": "final_answer",
                            "tool_calls": [],
                            "answer": {"status": "<answer status>", "claims": [], "gaps": []},
                        },
                    },
                }
            )
        prompt = self.prompts.render(
            "answer_compose" if state.phase == Phase.FINAL else "planner_decide",
            variables,
            tool_schema=schema,
            output_schema=PlannerResponse.model_json_schema(),
        )
        return PlannerRequest(
            asset_id=question.asset_id,
            analysis_id=question.analysis_id,
            question_id=question.question_id,
            job_id=state.job_id,
            prompt_text=prompt.rendered_text,
            prompt_bundle_hash=prompt.bundle_hash,
            available_evidence_ids=list(
                dict.fromkeys(e for o in evidence.observations for e in o.evidence_ids)
            ),
        )
