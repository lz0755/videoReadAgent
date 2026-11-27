"""M4 text-only PlannerProvider for the configured Chat Completions endpoint."""

import json
from typing import Any, Literal

from pydantic import Field, field_validator

from video_agent.adapters.llm import deepseek
from video_agent.adapters.llm.openai_slice import _Answer
from video_agent.agent.tools import CompareArgs, InspectArgs
from video_agent.application.evidence_service import SearchArgs
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import Answer, StrictModel
from video_agent.domain.ports import PlannerRequest, PlannerResponse, ToolCallProposal

_ARGUMENT_TYPES: dict[str, type[StrictModel]] = {
    "search_evidence": SearchArgs,
    "inspect_clip": InspectArgs,
    "compare_frames": CompareArgs,
}


class _Tool(StrictModel):
    tool_name: Literal["search_evidence", "inspect_clip", "compare_frames"]
    arguments: str = Field(description="JSON-encoded object matching this tool's argument schema.")

    @field_validator("arguments", mode="before")
    @classmethod
    def _encode_object(cls, value: object) -> object:
        # JSON Object mode can supply the object directly. Strict Structured Outputs
        # uses its JSON spelling to keep the response schema inside the reserved envelope.
        return json.dumps(value, allow_nan=False) if isinstance(value, dict) else value

    def proposal(self, server_id: str) -> ToolCallProposal:
        arguments = json.loads(self.arguments)
        validated = _ARGUMENT_TYPES[self.tool_name].model_validate(arguments)
        return ToolCallProposal(
            tool_call_id=server_id,
            tool_name=self.tool_name,
            arguments=validated.model_dump(exclude_unset=True),
        )


class _Decision(StrictModel):
    action: Literal["call_tools", "final_answer"]
    tool_calls: list[_Tool]
    answer: _Answer | None


class QAPlannerProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        model_id: str,
        api_key_env: str,
        cloud_upload_enabled: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.model_id = model_id
        self._key_env = api_key_env
        self._consent = cloud_upload_enabled
        if provider_name == "deepseek" and model_id != "deepseek-flash":
            raise ProviderError(ErrorCode.CAPABILITY_MISMATCH, "unapproved QA planner model")
        self._endpoint = deepseek.slice_endpoint(provider_name, model_id, api_key_env)

    async def decide(self, request: PlannerRequest) -> PlannerResponse:
        key = deepseek._check_cloud_access(
            cloud_upload_enabled=self._consent, api_key_env=self._key_env
        )
        payload = deepseek._chat_payload(
            self.provider_name, self.model_id, request.prompt_text, vision=False
        )
        if self.provider_name == "openai":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "qa_plan",
                    "strict": True,
                    "schema": _Decision.model_json_schema(),
                },
            }
        completion = await deepseek._complete(key, self._endpoint, request.timeout_seconds, payload)
        receipt = getattr(completion, "id", None)
        inputs, outputs, cost_status = deepseek._usage(completion)
        try:
            if not isinstance(receipt, str) or not receipt.strip():
                raise ValueError("missing completion ID")
            choice = completion.choices[0] if completion.choices else None
            message = getattr(choice, "message", None)
            raw = getattr(message, "content", None)
            if (
                not isinstance(raw, str)
                or getattr(message, "refusal", None)
                or getattr(choice, "finish_reason", None) in {"length", "content_filter"}
            ):
                raise ValueError("incomplete decision")
            decision = self._parse(raw)
            calls = [
                item.proposal(f"{receipt}-{index}")
                for index, item in enumerate(decision.tool_calls)
            ]
            if decision.action == "call_tools":
                if not calls:
                    raise ValueError("missing tool proposal")
            elif calls or decision.answer is None:
                raise ValueError("invalid final decision")
            answer = None
            if decision.answer is not None:
                answer = Answer.model_validate(
                    {
                        **decision.answer.model_dump(),
                        "asset_id": request.asset_id,
                        "analysis_id": request.analysis_id,
                        "question_id": request.question_id,
                        "answer_id": f"ans_{request.question_id}",
                        "trace_id": receipt,
                    }
                )
            return PlannerResponse(
                action=decision.action,
                tool_calls=calls,
                answer=answer,
                raw_text=raw,
                provider=self.provider_name,
                model_id=getattr(completion, "model", None) or self.model_id,
                provider_request_id=receipt,
                input_tokens=inputs,
                output_tokens=outputs,
                cost_status=cost_status,
            )
        except ValueError, ProviderError:
            raise ProviderError(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                "invalid QA planner response",
                details={
                    "provider_request_id": receipt if isinstance(receipt, str) else None,
                    "input_tokens": inputs,
                    "output_tokens": outputs,
                    "cost_status": cost_status,
                },
            ) from None

    @staticmethod
    def _parse(raw: str) -> _Decision:
        body: dict[str, Any] = deepseek._parse_json_content(raw)
        if "action" in body:
            return _Decision.model_validate(body)
        # The registered final-composition prompt also permits the bare answer shape.
        if body.pop("schema_version", "1.1") != "1.1":
            raise ValueError("unsupported answer schema")
        return _Decision(action="final_answer", tool_calls=[], answer=_Answer.model_validate(body))
