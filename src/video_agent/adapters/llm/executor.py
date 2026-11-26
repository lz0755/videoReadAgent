"""Execute one bound operation with durable, individually charged transport attempts."""

from collections.abc import Awaitable, Callable

from video_agent.application.budget import BudgetGuard
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import RequestBudget
from video_agent.domain.policies import should_attempt_schema_repair, should_retry_transient
from video_agent.domain.ports import (
    PlannerProvider,
    PlannerRequest,
    PlannerResponse,
    VisionProvider,
    VisionRequest,
    VisionResponse,
)


class ProviderExecutor:
    def __init__(self, guard: BudgetGuard, *, authorize: Callable[[], None]) -> None:
        self._guard = guard
        self._authorize = authorize

    async def observe(
        self,
        provider: VisionProvider,
        request: VisionRequest,
        *,
        scope_id: str,
        operation_key: str,
        explicit_retry: bool = False,
        parent_tool_call_id: str | None = None,
    ) -> VisionResponse:
        return await self._execute(
            provider.observe,
            provider.provider_name,
            provider.model_id,
            request,
            scope_id,
            operation_key,
            explicit_retry,
            len(request.images),
            parent_tool_call_id,
            False,
        )

    async def decide(
        self,
        provider: PlannerProvider,
        request: PlannerRequest,
        *,
        operation_key: str,
        explicit_retry: bool = False,
        final: bool = False,
    ) -> PlannerResponse:
        return await self._execute(
            provider.decide,
            provider.provider_name,
            provider.model_id,
            request,
            request.question_id,
            operation_key,
            explicit_retry,
            0,
            None,
            final,
        )

    async def _execute[R: (VisionRequest, PlannerRequest), T: (VisionResponse, PlannerResponse)](
        self,
        send: Callable[[R], Awaitable[T]],
        provider: str,
        model_id: str,
        request: R,
        scope_id: str,
        operation_key: str,
        explicit_retry: bool,
        image_count: int,
        parent: str | None,
        final: bool,
    ) -> T:
        guard = self._guard
        limits = guard.get_account(scope_id).limits
        retries = repairs = 0
        previous: str | None = None
        while True:
            self._authorize()
            budget = RequestBudget(
                scope_id=scope_id,
                job_id=request.job_id,
                provider=provider,
                model_id=model_id,
                image_count=image_count,
                is_final_synthesis=final,
                parent_tool_call_id=parent,
                estimated_cost_usd=guard.estimate_slice_request(
                    model_id, request.prompt_text, image_count
                ),
            )
            reserved = guard.reserve_provider_attempt(
                budget,
                operation_key=operation_key,
                explicit_retry=explicit_retry and previous is None,
                previous_attempt_id=previous,
            )
            try:
                outgoing = guard.prepare_provider_send(
                    request.model_copy(update={"attempt_id": reserved.attempt_id}),
                    provider=provider,
                    model_id=model_id,
                )
            except BaseException:
                guard.settle_not_sent(reserved, error_code=ErrorCode.BUDGET_EXCEEDED)
                raise
            try:
                result = await send(outgoing)
            except ProviderError as exc:
                if exc.code in (ErrorCode.PROVIDER_TIMEOUT, ErrorCode.EXTERNAL_RESULT_UNKNOWN):
                    guard.settle_unknown(reserved, error_code=exc.code)
                    raise
                guard.settle_failed(reserved, error_code=exc.code)
                if should_retry_transient(
                    exc.code,
                    transient_retries_used=retries,
                    max_transient_retries=limits.max_transient_retries,
                ):
                    retries += 1
                elif should_attempt_schema_repair(
                    exc.code, repairs_used=repairs, max_schema_repairs=limits.max_schema_repairs
                ):
                    repairs += 1
                    request = request.model_copy(
                        update={
                            "prompt_text": request.prompt_text
                            + "\nReturn the requested JSON schema. Cite only listed evidence IDs."
                        }
                    )
                else:
                    raise
                previous = reserved.attempt_id
                continue
            except BaseException:
                guard.settle_unknown(reserved)
                raise
            confirmed_free = result.cost_status == "confirmed" and reserved.reserved_cost_usd == 0
            guard.settle_success(
                reserved,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_request_id=result.provider_request_id,
                missing_usage=result.cost_status == "unknown",
                confirmed_cost_usd=0.0 if confirmed_free else None,
            )
            return result
