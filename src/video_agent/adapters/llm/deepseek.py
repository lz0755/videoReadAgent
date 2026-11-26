"""Thin M1.5.2 Chat Completions adapters shared by the DeepSeek and OpenAI routes.

Zero import-time network, API key lookup, or SDK client initialization.
Enforces explicit cloud_upload_enabled consent, API key presence, sdk_max_retries=0,
48 MiB serialized request body limit, 15-image 4096px dimension limit, and U+0020
blank JSON fallback interception.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from video_agent.adapters.llm.openai_slice import (
    OPENAI_BASE_URL,
    OPENAI_MODEL_ID,
    openai_completion_options,
)
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import Answer, Claim, CostStatus, Observation, TimeRangeMs
from video_agent.domain.policies import SLICE_MAX_OUTPUT_TOKENS
from video_agent.domain.ports import (
    ArtifactStore,
    PlannerRequest,
    PlannerResponse,
    VisionRequest,
    VisionResponse,
)

DEFAULT_DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
MAX_INLINE_REQUEST_BODY_BYTES: int = 50_331_648  # 48 MiB exact
IMAGE_COUNT_DIMENSION_THRESHOLD: int = 15
MAX_IMAGE_SIDE_AT_OR_ABOVE_15: int = 4096
MAX_IMAGE_SIDE_UNDER_15: int = 8192


def slice_endpoint(provider: str, model: str, key_env: str, base_url: str | None = None) -> str:
    if (provider, model, key_env) == ("openai", OPENAI_MODEL_ID, "OPENAI_API_KEY"):
        endpoint = OPENAI_BASE_URL
    elif provider == "deepseek" and key_env == "DEEPSEEK_API_KEY":
        endpoint = DEFAULT_DEEPSEEK_BASE_URL
    else:
        raise ProviderError(
            ErrorCode.CAPABILITY_MISMATCH,
            "unrecognized slice provider, model or credential binding",
        )
    if base_url is not None and base_url.rstrip("/") != endpoint:
        raise ProviderError(ErrorCode.CAPABILITY_MISMATCH, "unexpected slice provider endpoint")
    return endpoint


def _chat_payload(
    provider: str, model: str, content: str | list[dict[str, Any]], *, vision: bool
) -> dict[str, Any]:
    options = (
        openai_completion_options(vision=vision)
        if provider == "openai"
        else {"max_tokens": SLICE_MAX_OUTPUT_TOKENS, "response_format": {"type": "json_object"}}
    )
    return {"model": model, "messages": [{"role": "user", "content": content}], **options}


async def _complete(api_key: str, base_url: str, timeout: float, payload: dict[str, Any]) -> Any:
    import openai

    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        timeout=timeout,
        http_client=openai.DefaultAsyncHttpxClient(follow_redirects=False, timeout=timeout),
    )
    try:
        return await client.chat.completions.create(**payload, timeout=timeout)
    except Exception as exc:
        raise _map_provider_exception(exc) from exc
    finally:
        await client.close()


def _check_cloud_access(*, cloud_upload_enabled: bool, api_key_env: str) -> str:
    if not cloud_upload_enabled:
        raise ProviderError(
            ErrorCode.CLOUD_DISABLED,
            "cloud egress is disabled; explicit cloud_egress_authorized consent is required",
        )
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ProviderError(
            ErrorCode.CREDENTIAL_MISSING,
            f"required provider credential environment variable '{api_key_env}' is not set",
        )
    return api_key


def _map_provider_exception(exc: Exception) -> ProviderError:
    status_code = getattr(exc, "status_code", None)
    if status_code in {401, 402, 403}:
        return ProviderError(
            ErrorCode.PROVIDER_AUTH_FAILED,
            f"provider rejected authentication/authorization (HTTP {status_code})",
        )
    if status_code == 429:
        return ProviderError(
            ErrorCode.RATE_LIMITED,
            "provider returned HTTP 429 rate limit",
        )
    if status_code == 408:
        return ProviderError(
            ErrorCode.PROVIDER_TIMEOUT,
            "provider returned HTTP 408 timeout",
        )
    if status_code in {500, 502, 503, 504}:
        return ProviderError(
            ErrorCode.PROVIDER_TRANSIENT_ERROR,
            f"provider transient server error (HTTP {status_code})",
        )
    if status_code in {400, 413, 422}:
        return ProviderError(
            ErrorCode.CAPABILITY_MISMATCH,
            f"provider rejected request payload/parameters (HTTP {status_code})",
        )
    name = type(exc).__name__
    if "Timeout" in name:
        return ProviderError(
            ErrorCode.EXTERNAL_RESULT_UNKNOWN,
            f"provider request timed out with unknown remote outcome ({name})",
        )
    return ProviderError(
        ErrorCode.EXTERNAL_RESULT_UNKNOWN,
        f"provider transport failure with unknown remote outcome ({name})",
    )


def _usage(completion: Any) -> tuple[int, int, CostStatus]:
    usage = getattr(completion, "usage", None)
    inputs = getattr(usage, "prompt_tokens", None)
    outputs = getattr(usage, "completion_tokens", None)
    if type(inputs) is int and inputs > 0 and type(outputs) is int and outputs >= 0:
        return inputs, outputs, "estimated"
    return 0, 0, "unknown"


def _parse_json_content(raw_text: str | None) -> dict[str, Any]:
    if raw_text is None or not raw_text.strip():
        raise ProviderError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            "provider returned empty or U+0020 whitespace-only JSON response",
        )
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            f"provider returned invalid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            "provider JSON root must be an object",
        )
    return parsed


class DeepSeekVisionProvider:
    """Thin live VisionProvider targeting DeepSeek OpenAI-compatible Chat Completions."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        model_id: str,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str | None = None,
        cloud_upload_enabled: bool = False,
        provider_name: str = "deepseek",
    ) -> None:
        self._artifact_store = artifact_store
        self.provider_name = provider_name
        self.model_id = model_id
        self._api_key_env = api_key_env
        self._base_url = slice_endpoint(provider_name, model_id, api_key_env, base_url)
        self._cloud_upload_enabled = cloud_upload_enabled

    async def observe(self, request: VisionRequest) -> VisionResponse:
        api_key = _check_cloud_access(
            cloud_upload_enabled=self._cloud_upload_enabled,
            api_key_env=self._api_key_env,
        )

        image_count = len(request.images)
        max_side = (
            MAX_IMAGE_SIDE_AT_OR_ABOVE_15
            if image_count >= IMAGE_COUNT_DIMENSION_THRESHOLD
            else MAX_IMAGE_SIDE_UNDER_15
        )
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt_text}]
        for img in request.images:
            if max(img.width, img.height) > max_side:
                raise ProviderError(
                    ErrorCode.CAPABILITY_MISMATCH,
                    (
                        f"image {img.evidence_id} ({img.width}x{img.height}) exceeds provider "
                        f"max side {max_side}px for {image_count} image(s)"
                    ),
                )
            raw_bytes = self._artifact_store.read_bytes(img.artifact_id)
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.mime_type};base64,{b64}",
                        "detail": img.detail,
                    },
                }
            )

        payload_dict = _chat_payload(self.provider_name, self.model_id, content_parts, vision=True)
        serialized_size = len(json.dumps(payload_dict, ensure_ascii=False).encode("utf-8"))
        if serialized_size > MAX_INLINE_REQUEST_BODY_BYTES:
            raise ProviderError(
                ErrorCode.CAPABILITY_MISMATCH,
                (
                    f"serialized vision request body ({serialized_size} bytes) exceeds "
                    f"48 MiB limit ({MAX_INLINE_REQUEST_BODY_BYTES} bytes)"
                ),
            )

        completion = await _complete(api_key, self._base_url, request.timeout_seconds, payload_dict)

        try:
            return self._observations(request, completion)
        except ProviderError as exc:
            inputs, outputs, status = _usage(completion)
            exc.details.update(
                provider_request_id=getattr(completion, "id", None),
                input_tokens=inputs,
                output_tokens=outputs,
                cost_status=status,
            )
            raise

    def _observations(self, request: VisionRequest, completion: Any) -> VisionResponse:
        raw_text = completion.choices[0].message.content if completion.choices else ""
        parsed = _parse_json_content(raw_text)
        raw_obs_list = parsed.get("observations")
        if not isinstance(raw_obs_list, list) or not raw_obs_list:
            raise ProviderError(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                "vision JSON response must contain a non-empty 'observations' list",
            )

        pts_values = [img.actual_pts_ms for img in request.images]
        default_start = min(pts_values)
        default_end = max(max(pts_values) + 1, default_start + 1)
        response_model = str(getattr(completion, "model", None) or self.model_id)
        req_id = str(getattr(completion, "id", None) or f"{self.provider_name}-vision-unknown")

        observations: list[Observation] = []
        for idx, item in enumerate(raw_obs_list, start=1):
            if not isinstance(item, dict):
                raise ProviderError(
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    "each observation item must be a JSON object",
                )
            start_ms = item.get("start_ms", default_start)
            end_ms = item.get("end_ms", default_end)
            ev_ids = item.get("evidence_ids")
            if not isinstance(ev_ids, list) or not ev_ids:
                raise ProviderError(
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    "each observation must cite non-empty evidence_ids",
                )
            try:
                observations.append(
                    Observation(
                        observation_id=f"obs_{request.analysis_id}_{request.images[0].evidence_id}_{idx:04d}",
                        asset_id=request.asset_id,
                        analysis_id=request.analysis_id,
                        kind=item.get("kind", "observed"),
                        text=str(item.get("text", "")),
                        time_range=TimeRangeMs(start_ms=start_ms, end_ms=end_ms),
                        evidence_ids=[str(eid) for eid in ev_ids],
                        region_hint=item.get("region_hint"),
                        provenance={
                            "provider": self.provider_name,
                            "model_id": response_model,
                            "provider_request_id": req_id,
                        },
                    )
                )
            except Exception as exc:
                raise ProviderError(
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    f"observation schema validation failed: {exc}",
                ) from exc

        input_tokens, output_tokens, cost_status = _usage(completion)
        return VisionResponse(
            observations=observations,
            raw_text=str(raw_text or ""),
            provider=self.provider_name,
            model_id=response_model,
            provider_request_id=req_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_status=cost_status,
        )


class DeepSeekPlannerProvider:
    """Thin live PlannerProvider targeting DeepSeek OpenAI-compatible Chat Completions."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str | None = None,
        cloud_upload_enabled: bool = False,
        provider_name: str = "deepseek",
    ) -> None:
        self.provider_name = provider_name
        self.model_id = model_id
        self._api_key_env = api_key_env
        self._base_url = slice_endpoint(provider_name, model_id, api_key_env, base_url)
        self._cloud_upload_enabled = cloud_upload_enabled

    async def decide(self, request: PlannerRequest) -> PlannerResponse:
        api_key = _check_cloud_access(
            cloud_upload_enabled=self._cloud_upload_enabled,
            api_key_env=self._api_key_env,
        )

        payload = _chat_payload(
            self.provider_name, self.model_id, request.prompt_text, vision=False
        )
        completion = await _complete(api_key, self._base_url, request.timeout_seconds, payload)

        raw_text = completion.choices[0].message.content if completion.choices else ""
        parsed = _parse_json_content(raw_text)
        response_model = str(getattr(completion, "model", None) or self.model_id)
        req_id = str(getattr(completion, "id", None) or f"{self.provider_name}-planner-unknown")

        raw_claims = parsed.get("claims", [])
        raw_gaps = parsed.get("gaps", [])
        if not isinstance(raw_claims, list) or not isinstance(raw_gaps, list):
            raise ProviderError(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                "planner JSON response must include list fields 'claims' and 'gaps'",
            )
        try:
            claims = [
                Claim(
                    text=str(c.get("text", "")),
                    evidence_ids=[str(eid) for eid in c.get("evidence_ids", [])],
                    support_kind=c.get("support_kind", "observed"),
                    limitations=[str(lim) for lim in c.get("limitations", [])],
                )
                for c in raw_claims
                if isinstance(c, dict)
            ]
            answer = Answer(
                answer_id=f"ans_{request.question_id}",
                asset_id=request.asset_id,
                analysis_id=request.analysis_id,
                question_id=request.question_id,
                status=parsed.get("status", "not_answerable"),
                claims=claims,
                gaps=[str(g) for g in raw_gaps],
                trace_id=req_id,
            )
        except Exception as exc:
            raise ProviderError(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                f"planner answer schema validation failed: {exc}",
            ) from exc

        input_tokens, output_tokens, cost_status = _usage(completion)
        return PlannerResponse(
            action="final_answer",
            tool_calls=[],
            answer=answer,
            raw_text=str(raw_text or ""),
            provider=self.provider_name,
            model_id=response_model,
            provider_request_id=req_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_status=cost_status,
        )
