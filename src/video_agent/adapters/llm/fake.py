"""Deterministic offline Fake VisionProvider and PlannerProvider adapters (M1.3)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal

from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import Answer, Claim, Observation, TimeRangeMs
from video_agent.domain.ports import (
    PlannerRequest,
    PlannerResponse,
    ToolCallProposal,
    VisionRequest,
    VisionResponse,
)

FakeLlmScenario = Literal[
    "normal",
    "schema_error",
    "fabricated_citation",
    "timeout",
    "rate_limit",
    "unknown_result",
]


def _next_scenario(scenarios: list[FakeLlmScenario], call_index: int) -> FakeLlmScenario:
    if not scenarios:
        return "normal"
    return scenarios[min(call_index, len(scenarios) - 1)]


def _raise_for_fault(scenario: FakeLlmScenario, *, role: str) -> None:
    if scenario == "schema_error":
        raise ProviderError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            f"fake {role} returned non-conforming JSON payload",
        )
    if scenario == "timeout":
        raise ProviderError(
            ErrorCode.PROVIDER_TIMEOUT,
            f"fake {role} request timed out before response",
        )
    if scenario == "rate_limit":
        raise ProviderError(
            ErrorCode.RATE_LIMITED,
            f"fake {role} received HTTP 429 rate limit",
        )
    if scenario == "unknown_result":
        raise ProviderError(
            ErrorCode.EXTERNAL_RESULT_UNKNOWN,
            f"fake {role} request was sent but remote outcome is unknown",
        )


class FakeVisionProvider:
    """Deterministic async Fake implementing VisionProvider."""

    provider_name: str = "fake"
    model_id: str = "fake-vision"

    def __init__(
        self,
        *,
        scenarios: Sequence[FakeLlmScenario] = ("normal",),
        duration_ms: int = 60_000,
    ) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise ProviderError(
                ErrorCode.INVALID_TIME_RANGE,
                "FakeVisionProvider duration_ms must be a positive integer",
            )
        self.calls: list[VisionRequest] = []
        self._scenarios = list(scenarios)
        self._duration_ms = duration_ms

    async def observe(self, request: VisionRequest) -> VisionResponse:
        call_idx = len(self.calls)
        self.calls.append(request)
        scenario = _next_scenario(self._scenarios, call_idx)
        _raise_for_fault(scenario, role="vision")

        pts_values = [img.actual_pts_ms for img in request.images]
        for pts in pts_values:
            if pts < 0 or pts >= self._duration_ms:
                raise ProviderError(
                    ErrorCode.INVALID_TIME_RANGE,
                    (
                        f"frame actual_pts_ms ({pts}) is outside valid half-open video "
                        f"bounds [0, {self._duration_ms})"
                    ),
                )

        start_ms = min(pts_values)
        end_ms = max(max(pts_values) + 1, start_ms + 1)
        time_range = TimeRangeMs(start_ms=start_ms, end_ms=end_ms)
        time_range.validate_within(self._duration_ms)

        if scenario == "fabricated_citation":
            evidence_ids = ["ev_fabricated_nonexistent_999"]
            text = "Fabricated citation test observation."
        else:
            evidence_ids = [img.evidence_id for img in request.images]
            text = f"Observed {len(request.images)} frame(s) between {start_ms}ms and {end_ms}ms."

        obs = Observation(
            observation_id=f"obs_fake_{call_idx + 1:04d}",
            asset_id=request.asset_id,
            analysis_id=request.analysis_id,
            kind="observed",
            text=text,
            time_range=time_range,
            evidence_ids=evidence_ids,
            provenance={"provider": self.provider_name, "model_id": self.model_id},
        )
        raw_payload = json.dumps({"observations": [obs.model_dump()]}, ensure_ascii=False)
        return VisionResponse(
            observations=[obs],
            raw_text=raw_payload,
            provider=self.provider_name,
            model_id=self.model_id,
            provider_request_id=f"fake-vision-req-{call_idx + 1:04d}",
            input_tokens=128 * len(request.images),
            output_tokens=64,
            cost_status="confirmed",
        )


class FakePlannerProvider:
    """Deterministic async Fake implementing PlannerProvider."""

    provider_name: str = "fake"
    model_id: str = "fake-planner"

    def __init__(
        self,
        *,
        scenarios: Sequence[FakeLlmScenario] = ("normal",),
        mode: Literal["final_answer", "call_tools"] = "final_answer",
    ) -> None:
        self.calls: list[PlannerRequest] = []
        self._scenarios = list(scenarios)
        self._mode = mode

    async def decide(self, request: PlannerRequest) -> PlannerResponse:
        call_idx = len(self.calls)
        self.calls.append(request)
        scenario = _next_scenario(self._scenarios, call_idx)
        _raise_for_fault(scenario, role="planner")

        req_id = f"fake-planner-req-{call_idx + 1:04d}"
        if self._mode == "call_tools" and scenario == "normal":
            proposal = ToolCallProposal(
                tool_call_id=f"tc_fake_{call_idx + 1:04d}",
                tool_name="search_evidence",
                arguments={"query": "孔径", "limit": 5},
            )
            return PlannerResponse(
                action="call_tools",
                tool_calls=[proposal],
                answer=None,
                raw_text=json.dumps({"action": "call_tools"}, ensure_ascii=False),
                provider=self.provider_name,
                model_id=self.model_id,
                provider_request_id=req_id,
                input_tokens=200,
                output_tokens=40,
                cost_status="confirmed",
            )

        if scenario == "fabricated_citation":
            answer = Answer(
                answer_id=f"ans_fake_{call_idx + 1:04d}",
                asset_id=request.asset_id,
                analysis_id=request.analysis_id,
                question_id=request.question_id,
                status="answered",
                claims=[
                    Claim(
                        text="Unsupported claim citing nonexistent evidence.",
                        evidence_ids=["ev_fabricated_planner_999"],
                        support_kind="observed",
                    )
                ],
                gaps=[],
                trace_id=f"trace_fake_{call_idx + 1:04d}",
            )
        elif request.available_evidence_ids:
            answer = Answer(
                answer_id=f"ans_fake_{call_idx + 1:04d}",
                asset_id=request.asset_id,
                analysis_id=request.analysis_id,
                question_id=request.question_id,
                status="answered",
                claims=[
                    Claim(
                        text="Grounded offline fake answer citing provided evidence.",
                        evidence_ids=[request.available_evidence_ids[0]],
                        support_kind="observed",
                    )
                ],
                gaps=[],
                trace_id=f"trace_fake_{call_idx + 1:04d}",
            )
        else:
            answer = Answer(
                answer_id=f"ans_fake_{call_idx + 1:04d}",
                asset_id=request.asset_id,
                analysis_id=request.analysis_id,
                question_id=request.question_id,
                status="not_answerable",
                claims=[],
                gaps=["no evidence available in request context"],
                trace_id=f"trace_fake_{call_idx + 1:04d}",
            )

        return PlannerResponse(
            action="final_answer",
            tool_calls=[],
            answer=answer,
            raw_text=answer.model_dump_json(),
            provider=self.provider_name,
            model_id=self.model_id,
            provider_request_id=req_id,
            input_tokens=256,
            output_tokens=96,
            cost_status="confirmed",
        )
