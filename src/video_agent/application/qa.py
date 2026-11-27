"""Run one scoped question against the published index and its QA budget."""

from video_agent.agent.answer_validator import AnswerValidator
from video_agent.agent.controller import Controller
from video_agent.agent.state import PlannerState
from video_agent.application.event_localization import EventLocalizer
from video_agent.domain.errors import ErrorCode
from video_agent.domain.models import Answer
from video_agent.domain.ports import SearchHit, ToolCallProposal


async def answer_question(
    job_id: str, controller: Controller, localizer: EventLocalizer, validator: AnswerValidator
) -> tuple[Answer, list[SearchHit]]:
    executor = controller.executor
    question = executor.service.question
    initial = await executor.execute(
        ToolCallProposal(
            tool_call_id="initial",
            tool_name="search_evidence",
            arguments={"query": question.query_text},
        )
    )
    approval = executor.guard.run_limits
    if approval is not None and approval.max_provider_attempts == 0:
        answer = PlannerState(question, job_id, initial).stop(ErrorCode.BUDGET_EXCEEDED.value)
    elif question.mode == "event_localization":
        answer, context = await localizer.run(job_id, initial)
        validator.validate(answer, context)
    else:
        answer = await controller.run(job_id, initial)
    # The answer primary key and trace always identify this execution, even for a
    # provider that restarts its own response numbering at one on each invocation.
    bound = answer.model_copy(
        update={"answer_id": "ans_" + question.question_id, "trace_id": job_id}
    )
    return bound, initial.hits
