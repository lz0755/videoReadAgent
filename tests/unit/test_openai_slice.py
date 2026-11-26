"""OpenAI selection, transport and price reservation through the M1.5 entry points."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import openai
import pytest
import yaml

from eval import slice_runner
from video_agent.bootstrap import build_slice_container
from video_agent.cli import main as cli_main
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import RequestBudget
from video_agent.domain.policies import SLICE_MAX_OUTPUT_TOKENS
from video_agent.domain.ports import ImageInput, PlannerRequest, VisionRequest

MODEL = "gpt-6-luna"


@pytest.fixture
def approval(tmp_path: Path) -> Path:
    path = tmp_path / "approval.yaml"
    path.write_text(
        "live_enabled: true\ncloud_egress_authorized: true\nmax_model_attempts: 2\n",
        encoding="utf-8",
    )
    return path


def app_config(tmp_path: Path, provider: str, model: str, key_env: str) -> Path:
    raw = yaml.safe_load(Path("config/defaults.yaml").read_text(encoding="utf-8"))
    raw["provider"].update(name=provider, model=model, api_key_env=key_env)
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize("entry_point", ["manifest", "single"])
def test_openai_cli_requires_its_own_key(
    tmp_path: Path, approval: Path, monkeypatch: pytest.MonkeyPatch, entry_point: str
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-deepseek-fixture")
    args = ["--live", "--provider", "openai", "--budget-config", str(approval)]
    with pytest.raises(ProviderError) as caught:
        if entry_point == "manifest":
            slice_runner.main([*args, "--output-dir", str(tmp_path), "--data-root", str(tmp_path)])
        else:
            cli_main(
                [
                    "slice",
                    "--video",
                    "fixture.mp4",
                    "--question",
                    "Q",
                    "--data-root",
                    str(tmp_path),
                    *args,
                ]
            )
    assert caught.value.code is ErrorCode.CREDENTIAL_MISSING
    assert "OPENAI_API_KEY" in str(caught.value)


def test_openai_key_cannot_be_bound_to_deepseek(
    tmp_path: Path, approval: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-openai-fixture")
    with pytest.raises(ProviderError) as caught:
        build_slice_container(
            mode="live",
            data_root=tmp_path,
            budget_config_path=approval,
            config_path=app_config(tmp_path, "deepseek", "deepseek-flash", "OPENAI_API_KEY"),
        )
    assert caught.value.code is ErrorCode.CAPABILITY_MISMATCH


@pytest.mark.parametrize("vision", [True, False], ids=["vision", "qa"])
def test_openai_transport_uses_official_origin_schema_and_budget(
    tmp_path: Path, approval: Path, monkeypatch: pytest.MonkeyPatch, vision: bool
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-openai-fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://invalid.example/v1")
    container = build_slice_container(
        mode="live",
        data_root=tmp_path,
        budget_config_path=approval,
        config_path=app_config(tmp_path, "openai", MODEL, "OPENAI_API_KEY"),
    )
    view: dict[str, Any] = {"kind": "observed", "text": "circle", "evidence_ids": ["ev1"]}
    outputs = (
        {"observations": [view | {"start_ms": 0, "end_ms": 1}]}
        if vision
        else {"status": "not_answerable", "claims": [], "gaps": ["missing evidence"]}
    )
    completion = SimpleNamespace(
        id="chatcmpl-fixture",
        model=MODEL,
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(outputs)))],
        usage=SimpleNamespace(prompt_tokens=95, completion_tokens=40),
    )
    sdk = Mock()
    sdk.return_value.chat.completions.create = AsyncMock(return_value=completion)
    sdk.return_value.close = AsyncMock()
    monkeypatch.setattr(openai, "AsyncOpenAI", sdk)
    guard = container.budget_guard
    scope = "analysis_1" if vision else "question_1"
    guard.ensure_started(scope, "analysis" if vision else "qa")
    reservation = guard.reserve_provider_attempt(
        RequestBudget(
            scope_id=scope,
            job_id="job",
            provider="openai",
            model_id=MODEL,
            image_count=int(vision),
            is_final_synthesis=not vision,
        )
    )
    common: dict[str, Any] = dict(
        asset_id="asset",
        analysis_id="analysis_1",
        job_id="job",
        prompt_text="{}",
        prompt_bundle_hash="hash",
        attempt_id=reservation.attempt_id,
    )
    if vision:
        image = ImageInput(
            evidence_id="ev1",
            artifact_id=container.artifact_store.put_bytes(
                "asset", "frame", b"test-image", suffix=".jpg"
            ),
            width=1,
            height=1,
            actual_pts_ms=0,
        )
        observed = slice_runner._run_offline_coro(
            container.vision_provider.observe(VisionRequest(images=[image], **common))
        )
        assert observed.observations[0].provenance["provider_request_id"] == completion.id
    else:
        response = slice_runner._run_offline_coro(
            container.planner_provider.decide(PlannerRequest(question_id=scope, **common))
        )
        assert response.answer and response.answer.trace_id == completion.id

    assert str(sdk.call_args.kwargs["base_url"]).rstrip("/") == "https://api.openai.com/v1"
    assert sdk.call_args.kwargs["api_key"] == "offline-openai-fixture"
    assert sdk.call_args.kwargs["max_retries"] == 0
    payload = sdk.return_value.chat.completions.create.call_args.kwargs
    assert payload["model"] == MODEL and payload["store"] is False
    assert (
        payload["max_completion_tokens"] == SLICE_MAX_OUTPUT_TOKENS and "max_tokens" not in payload
    )
    assert payload["service_tier"] == "default"
    assert payload["prompt_cache_options"] == {"mode": "explicit"}
    assert payload["response_format"]["type"] == "json_schema"
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True and schema["schema"]["additionalProperties"] is False
    required = {"observations"} if vision else {"status", "claims", "gaps"}
    assert required <= set(schema["schema"]["required"])
    price = container.prices.models[MODEL]
    assert (price.input_per_1m_tokens_usd, price.output_per_1m_tokens_usd) == (0.10, 0.50)
    assert "openai.com/" in container.prices.source
    envelope_size = len(json.dumps(payload["response_format"]).encode("utf-8"))
    expected_input = 2 + envelope_size + int(vision) * price.estimated_tokens_per_image
    assert (
        guard.estimate_slice_request(MODEL, "{}", int(vision))
        >= (expected_input * 0.10 + SLICE_MAX_OUTPUT_TOKENS * 0.50) / 1e6
    )
