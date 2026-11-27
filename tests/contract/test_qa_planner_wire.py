import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import openai
import pytest

from tests.unit.test_budget_guard import _make_guard, _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.openai_slice import OPENAI_MODEL_ID
from video_agent.adapters.llm.qa_planner import QAPlannerProvider
from video_agent.bootstrap import load_price_config
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.policies import SLICE_MAX_OUTPUT_TOKENS
from video_agent.domain.ports import PlannerRequest


def request() -> PlannerRequest:
    return PlannerRequest(
        question_id="q",
        asset_id="asset",
        analysis_id="analysis",
        job_id="job",
        prompt_text="registered JSON planner prompt",
        prompt_bundle_hash="verified-hash",
        timeout_seconds=6.25,
        available_evidence_ids=["ev"],
    )


def completion(content: object, *, usage: bool = True) -> SimpleNamespace:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return SimpleNamespace(
        id="actual-receipt",
        model="model-revision",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=744, completion_tokens=17) if usage else None,
    )


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock()
    client.return_value.close = AsyncMock()
    client.return_value.chat.completions.create = AsyncMock()
    monkeypatch.setattr(openai, "AsyncOpenAI", client)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unused.invalid")
    return client


def deepseek(*, consent: bool = True) -> QAPlannerProvider:
    return QAPlannerProvider(
        provider_name="deepseek",
        model_id="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        cloud_upload_enabled=consent,
    )


def tools(arguments: object) -> dict[str, object]:
    return {
        "action": "call_tools",
        "answer": None,
        "tool_calls": [{"tool_name": "search_evidence", "arguments": arguments}],
    }


@pytest.mark.parametrize("provider", ["deepseek", "openai"])
def test_registered_prompt_proposals_and_bounded_sdk_request(sdk: Mock, provider: str) -> None:
    model, key, origin = (
        ("deepseek-flash", "DEEPSEEK_API_KEY", "https://api.deepseek.com")
        if provider == "deepseek"
        else (OPENAI_MODEL_ID, "OPENAI_API_KEY", "https://api.openai.com/v1")
    )
    port = QAPlannerProvider(
        provider_name=provider, model_id=model, api_key_env=key, cloud_upload_enabled=True
    )
    search_args = {"query": "孔径", "limit": 3}
    body = {
        "action": "call_tools",
        "answer": None,
        "tool_calls": [
            {
                "tool_name": "search_evidence",
                "arguments": (json.dumps(search_args) if provider == "openai" else search_args),
            },
            {"tool_name": "inspect_clip", "arguments": {"start_ms": 100, "end_ms": 500}},
            {"tool_name": "compare_frames", "arguments": {"first_ms": 100, "second_ms": 499}},
        ],
    }
    sdk.return_value.chat.completions.create.return_value = completion(body)
    result = _run_coro(port.decide(request()))
    assert result.action == "call_tools" and result.answer is None
    assert [t.tool_name for t in result.tool_calls] == [
        "search_evidence",
        "inspect_clip",
        "compare_frames",
    ]
    assert result.tool_calls[0].arguments == search_args
    assert len({c.tool_call_id for c in result.tool_calls}) == 3
    assert (
        result.provider_request_id,
        result.input_tokens,
        result.output_tokens,
        result.cost_status,
    ) == ("actual-receipt", 744, 17, "estimated")
    args = sdk.call_args.kwargs
    assert str(args["base_url"]).rstrip("/") == origin and args["max_retries"] == 0
    wire = sdk.return_value.chat.completions.create.call_args.kwargs
    assert wire["messages"] == [{"role": "user", "content": request().prompt_text}]
    assert wire["timeout"] == request().timeout_seconds
    assert wire["model"] == model
    if provider == "openai":
        assert wire["max_completion_tokens"] == SLICE_MAX_OUTPUT_TOKENS and wire["store"] is False
        fmt = wire["response_format"]
        schema = fmt["json_schema"]
        assert fmt["type"] == "json_schema" and schema["strict"] is True
        assert set(schema["schema"]["required"]) == {"action", "tool_calls", "answer"}
        # The configured cost estimate already reserves the entire schema envelope.
        overhead = (
            load_price_config(Path("config/prices_openai.yaml"))
            .models[model]
            .request_overhead_tokens
        )
        assert len(json.dumps(fmt).encode()) <= overhead
    else:
        assert wire["max_tokens"] == SLICE_MAX_OUTPUT_TOKENS
        assert wire["response_format"] == {"type": "json_object"}
    sdk.return_value.close.assert_awaited_once()


@pytest.mark.parametrize("bare", [True, False])
def test_final_composition_binds_answer_and_tracks_missing_usage(sdk: Mock, bare: bool) -> None:
    answer = {
        "status": "answered",
        "claims": [
            {
                "text": "visible",
                "support_kind": "observed",
                "evidence_ids": ["ev"],
                "limitations": [],
            }
        ],
        "gaps": [],
    }
    body = answer if bare else {"action": "final_answer", "tool_calls": [], "answer": answer}
    sdk.return_value.chat.completions.create.return_value = completion(body, usage=False)
    response = _run_coro(deepseek().decide(request()))
    assert response.action == "final_answer" and not response.tool_calls
    assert response.answer and response.answer.claims[0].evidence_ids == ["ev"]
    assert (
        response.answer.asset_id,
        response.answer.analysis_id,
        response.answer.question_id,
        response.answer.trace_id,
    ) == ("asset", "analysis", "q", "actual-receipt")
    assert response.cost_status == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        {"action": "shell", "tool_calls": [], "answer": None},
        tools({"query": "a", "asset_id": "other"}),
        tools({"query": "a", "url": "https://unused.invalid"}),
        tools({"query": "a", "limit": True}),
        {
            "action": "call_tools",
            "tool_calls": [{"tool_name": "get_evidence", "arguments": {}}],
            "answer": None,
        },
        {"action": "call_tools", "tool_calls": [], "answer": None},
        tools({"query": "a"}) | {"secret": "x"},
        {
            "action": "final_answer",
            "tool_calls": [],
            "answer": {
                "status": "not_answerable",
                "claims": [],
                "gaps": ["missing"],
                "question_id": "foreign",
            },
        },
        {"status": "answered", "claims": [], "gaps": []},
        "not json",
    ],
)
def test_invalid_output_preserves_known_usage_and_receipt(sdk: Mock, body: object) -> None:
    sdk.return_value.chat.completions.create.return_value = completion(body)
    with pytest.raises(ProviderError) as err:
        _run_coro(deepseek().decide(request()))
    assert err.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED
    assert err.value.details == {
        "provider_request_id": "actual-receipt",
        "input_tokens": 744,
        "output_tokens": 17,
        "cost_status": "estimated",
    }


def test_one_repair_and_failed_receipt_are_counted_by_provider_executor(sdk: Mock) -> None:
    create = sdk.return_value.chat.completions.create
    create.side_effect = [completion(" ", usage=False), completion(tools({"query": "孔径"}))]
    guard, _, _ = _make_guard()
    guard.ensure_started("q", "qa")
    result = _run_coro(
        ProviderExecutor(guard, authorize=lambda: None).decide(
            deepseek(), request(), operation_key="planner-1"
        )
    )
    assert result.tool_calls and create.await_count == 2
    ledger = guard.get_ledger("q")
    assert ledger.total_attempts == 2 and ledger.total_images_sent == 0
    assert [(r.status, r.cost_status, r.provider_request_id) for r in ledger.records] == [
        ("failed", "unknown", "actual-receipt"),
        ("succeeded", "estimated", "actual-receipt"),
    ]
    assert ledger.cost_status == "unknown"


@pytest.mark.parametrize(
    ("provider", "model", "key"),
    [
        ("openai", "deepseek-flash", "OPENAI_API_KEY"),
        ("deepseek", "deepseek-flash", "OPENAI_API_KEY"),
        ("deepseek", "unapproved-model", "DEEPSEEK_API_KEY"),
        ("other", OPENAI_MODEL_ID, "OPENAI_API_KEY"),
    ],
)
def test_rejects_unbound_provider_model_or_key_before_sending(
    sdk: Mock, provider: str, model: str, key: str
) -> None:
    with pytest.raises(ProviderError) as err:
        p = QAPlannerProvider(
            provider_name=provider, model_id=model, api_key_env=key, cloud_upload_enabled=True
        )
        _run_coro(p.decide(request()))
    assert err.value.code == ErrorCode.CAPABILITY_MISMATCH
    sdk.assert_not_called()


def test_egress_requires_consent_and_credentials(
    sdk: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProviderError) as err:
        _run_coro(deepseek(consent=False).decide(request()))
    assert err.value.code == ErrorCode.CLOUD_DISABLED
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ProviderError) as err:
        _run_coro(deepseek().decide(request()))
    assert err.value.code == ErrorCode.CREDENTIAL_MISSING
    sdk.assert_not_called()
