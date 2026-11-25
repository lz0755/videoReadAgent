"""M0.1 contract tests: capability status integrity, 14/15/16 x 4096/4097px boundaries,
software_tutorial A/B serialization, tool-image SDK vs doc distinction, retries, and Files API.

All tests run 100% offline under --disable-socket --allow-unix-socket (zero real network calls).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from experiments.probe_gate import (
    ProbeAuthorizationConfig,
    ProbeBudgetConfig,
    ProbeBudgetLedger,
    ProbeGateConfig,
    ProbeGateError,
)
from experiments.provider_probe import (
    DEFAULT_PROVIDER_CONFIG_PATH,
    CapabilityContractError,
    CapabilityEntry,
    ProviderContractConfig,
    ProviderProbeClient,
    build_initial_capability_entries,
    build_synthetic_software_tutorial_frame,
    crop_grid_roi_from_original,
    evaluate_image_boundary_case,
    execute_live_probe_suite,
    inspect_openai_sdk_tool_image_support,
    make_synthetic_boundary_image,
    parse_structured_json_response,
    prepare_software_tutorial_comparison,
    render_numbered_3x3_grid_preview,
    run_files_api_lifecycle_probe,
    run_offline_boundary_suite,
    run_offline_m0_1,
    validate_capability_matrix,
)


def _load_provider_config() -> ProviderContractConfig:
    return ProviderContractConfig.from_yaml(DEFAULT_PROVIDER_CONFIG_PATH)


def test_model_id_loaded_from_config_and_overrideable() -> None:
    cfg = _load_provider_config()
    assert cfg.model_id == "deepseek-flash"
    assert cfg.snapshot_date == "2026-11-25"

    overridden = ProviderContractConfig.from_yaml(
        DEFAULT_PROVIDER_CONFIG_PATH,
        model_id_override="custom-vision-model",
    )
    assert overridden.model_id == "custom-vision-model"


def test_offline_run_never_marks_documented_or_untested_as_runtime_verified(
    tmp_path: Path,
) -> None:
    cap_out = tmp_path / "capabilities.json"
    rep_out = tmp_path / "run_record.json"
    matrix, record = run_offline_m0_1(capabilities_out=cap_out, report_out=rep_out)

    assert cap_out.exists()
    assert rep_out.exists()
    validate_capability_matrix(matrix)

    statuses = {row["capability_id"]: row["status"] for row in matrix["capabilities"]}
    assert "runtime_verified" not in statuses.values()
    assert statuses["text_chat_completion"] == "documented_supported"
    assert statuses["vision_multi_image_order"] == "documented_supported"
    assert statuses["vision_detail_levels"] == "documented_supported"
    assert statuses["structured_candidate_json"] == "documented_supported"
    assert statuses["tool_calls_text_observation"] == "documented_supported"
    assert statuses["tool_calls_image_roundtrip"] == "documented_supported"
    assert statuses["files_api_upload_reuse_expiry_delete"] == "documented_supported"
    assert statuses["software_tutorial_high_vs_grid_roi"] == "not_tested"
    assert statuses["assistant_message_images"] == "unsupported"
    assert statuses["text_only_model_vision"] == "unsupported"
    assert record["budget_ledger"]["model_attempts"] == 0
    assert record["budget_ledger"]["provider_http_attempts"] == 0
    assert matrix["decision"]["verdict"] == "pause_pending_live_verification"


def test_validate_capability_matrix_blocks_false_runtime_verified_and_missing_error() -> None:
    bad_unexecuted = {
        "capabilities": [
            {
                "capability_id": "text_chat_completion",
                "status": "runtime_verified",
                "live_executed": False,
                "verification_passed": None,
                "runtime_evidence_id": None,
                "last_error": None,
            }
        ]
    }
    with pytest.raises(CapabilityContractError):
        validate_capability_matrix(bad_unexecuted)

    bad_missing_error = {
        "capabilities": [
            {
                "capability_id": "vision_detail_levels",
                "status": "documented_supported",
                "live_executed": True,
                "verification_passed": False,
                "runtime_evidence_id": None,
                "last_error": None,
            }
        ]
    }
    with pytest.raises(CapabilityContractError):
        validate_capability_matrix(bad_missing_error)


def test_first_failure_preserves_error_reason_and_never_marks_runtime_verified() -> None:
    entry = CapabilityEntry(
        capability_id="vision_detail_levels",
        description="detail parameter",
        status="documented_supported",
        documented_status="documented_supported",
        doc_source="https://api-docs.deepseek.com/guides/vision.md",
    )
    entry.record_runtime_failure(
        error_code="INVALID_PARAMETERS",
        http_status=422,
        message="detail value rejected",
        attempt_id="model-0001",
    )
    assert entry.status == "documented_supported"
    assert entry.to_dict()["status"] != "runtime_verified"
    assert entry.live_executed is True
    assert entry.verification_passed is False
    assert entry.failure_count == 1
    assert entry.last_error is not None
    assert entry.last_error.error_code == "INVALID_PARAMETERS"
    assert entry.last_error.http_status == 422
    assert "detail value rejected" in entry.last_error.message


def test_offline_14_15_16_image_4096_4097px_boundary_and_1080p_non_proof() -> None:
    cfg = _load_provider_config()
    suite = run_offline_boundary_suite(cfg)
    assert suite["proves_15_image_4096_contract_offline"] is True
    assert suite["ordinary_1080p_proves_4096_limit"] is False

    by_id = {c["case_id"]: c for c in suite["cases"]}
    # <= 14 images allow up to 8192px -> both 4096 and 4097 allowed
    assert by_id["14_images_4096px"]["allowed_by_documented_contract"] is True
    assert by_id["14_images_4096px"]["effective_max_side_px"] == 8192
    assert by_id["14_images_4097px"]["allowed_by_documented_contract"] is True
    assert by_id["14_images_4097px"]["effective_max_side_px"] == 8192

    # >= 15 images enforce 4096px side limit -> 4096 allowed, 4097 rejected
    assert by_id["15_images_4096px"]["allowed_by_documented_contract"] is True
    assert by_id["15_images_4096px"]["effective_max_side_px"] == 4096
    assert by_id["15_images_4097px"]["allowed_by_documented_contract"] is False
    assert "IMAGE_DIMENSION_LIMIT_EXCEEDED" in by_id["15_images_4097px"]["violations"]
    assert by_id["15_images_4097px"]["triggers_15_img_4096_rejection"] is True

    assert by_id["16_images_4096px"]["allowed_by_documented_contract"] is True
    assert by_id["16_images_4097px"]["allowed_by_documented_contract"] is False
    assert "IMAGE_DIMENSION_LIMIT_EXCEEDED" in by_id["16_images_4097px"]["violations"]

    # Ordinary 1080p succeeds at 15 images, but explicitly CANNOT prove 4096px boundary was tested
    case_1080p = by_id["15_images_ordinary_1080p_non_proof"]
    assert case_1080p["allowed_by_documented_contract"] is True
    assert case_1080p["exercises_15_img_4096_boundary"] is False
    assert case_1080p["ordinary_1080p_note"] is not None
    assert "ordinary_1080p_cannot_prove_4096px_limit" in case_1080p["ordinary_1080p_note"]

    # Oversized serialized body (> 48 MiB) rejected offline
    body_case = by_id["body_over_48mib_offline_guard"]
    assert body_case["allowed_by_documented_contract"] is False
    assert "REQUEST_BODY_LIMIT_EXCEEDED" in body_case["violations"]

    # Verify real synthetic PNG dimensions match 4096 / 4097 exactly
    img4097 = make_synthetic_boundary_image(4097, 64, label="check_4097")
    assert img4097.max_side_px == 4097
    direct = evaluate_image_boundary_case(
        case_id="direct_15x4097",
        model_id=cfg.model_id,
        images=[img4097] * 15,
        limits=cfg.limits,
    )
    assert direct["allowed_by_documented_contract"] is False


def test_software_tutorial_high_vs_960_grid_roi_serialization_and_ungridded_crop() -> None:
    cfg = _load_provider_config()
    frame = build_synthetic_software_tutorial_frame(1920, 1080)
    comp = prepare_software_tutorial_comparison(
        config=cfg,
        frame_image=frame,
        source_kind="synthetic_offline_fixture",
        roi_region_id="r2c2",
    )

    assert comp["high_guarantees_small_text_legibility"] is False
    strat_a = comp["strategy_a_original_high"]
    strat_b = comp["strategy_b_960_plus_grid_roi"]

    assert strat_a["image_dimensions"] == [[1920, 1080]]
    assert strat_a["serialized_request_body_bytes"] > strat_a["raw_image_bytes"]
    assert strat_a["within_48mib_limit"] is True

    assert strat_b["roi_region_id"] == "r2c2"
    assert strat_b["roi_cropped_from_ungridded_original"] is True
    assert strat_b["grid_preview_is_fact_evidence"] is False
    assert max(strat_b["image_dimensions"][0]) == 960
    assert strat_b["serialized_request_body_bytes"] > strat_b["raw_image_bytes"]

    # Verify ROI crop comes from the ungridded original, NOT from the red-grid preview
    gridded = render_numbered_3x3_grid_preview(frame)
    roi_from_orig, bbox = crop_grid_roi_from_original(frame, "r2c2")
    roi_from_grid, _ = crop_grid_roi_from_original(gridded, "r2c2")
    assert roi_from_orig.tobytes() != roi_from_grid.tobytes()
    assert (
        roi_from_orig.tobytes()
        == frame.crop((bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"])).tobytes()
    )


def test_tool_image_roundtrip_distinguishes_doc_vs_sdk_and_keeps_text_default() -> None:
    cfg = _load_provider_config()
    info = inspect_openai_sdk_tool_image_support(cfg.model_id)

    assert info["endpoint_documented_status"] == "documented_supported"
    assert info["endpoint_runtime_status"] == "documented_supported"
    assert info["p0_default_tool_return_mode"] == "text_observation_json"
    # OpenAI SDK static type omits image parts in ChatCompletionToolMessageParam
    assert info["sdk_static_type_supports_image_part"] is False
    # Yet untyped dict wire serialization preserves tool_call_id and image_url block
    assert info["sdk_offline_wire_serialization_verified"] is True
    assert info["wire_preserves_tool_call_id"] is True
    assert info["wire_preserves_image_url_part"] is True


def test_whitespace_only_json_fallback_detected() -> None:
    ok, obj, reason = parse_structured_json_response("       ")
    assert ok is False
    assert obj is None
    assert reason == "WHITESPACE_ONLY_JSON_FALLBACK"

    ok2, obj2, reason2 = parse_structured_json_response('{"kind":"observed","text":"20"}')
    assert ok2 is True
    assert obj2 == {"kind": "observed", "text": "20"}
    assert reason2 == "OK"


def test_retries_unknown_timeout_and_files_api_accounting_via_mock_transport() -> None:
    cfg = _load_provider_config()
    budget = ProbeBudgetConfig(
        max_model_attempts=12,
        max_provider_http_attempts=8,
        max_transferred_bytes=5_000_000,
        deadline_seconds=120.0,
        per_request_timeout_seconds=30.0,
        max_retries_per_request=2,
    )
    gate_cfg = ProbeGateConfig(
        authorization=ProbeAuthorizationConfig(
            cloud_egress_authorized=True,
            live_enabled=True,
            authorized_by="offline-mock-test",
            files_api_upload_authorized=True,
            files_api_ttl_seconds=3600,
        ),
        budget=budget,
    )
    ledger = ProbeBudgetLedger(budget=budget)
    call_counts: dict[str, int] = {"chat": 0, "files_upload": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "deepseek-flash",
                            "name": "DeepSeek-V4.1-Flash",
                            "context_window": 1048576,
                            "max_output_tokens": 393216,
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        }
                    ],
                },
            )
        if request.method == "POST" and path == "/files":
            call_counts["files_upload"] += 1
            if call_counts["files_upload"] == 1:
                # First upload hits transient 503 -> must retry and count BOTH HTTP attempts!
                return httpx.Response(503, text="overloaded")
            return httpx.Response(
                200,
                json={
                    "id": "file-api-mock123456",
                    "object": "file",
                    "bytes": 512,
                    "created_at": 1700000000,
                    "filename": "sample.png",
                    "purpose": "user_data",
                    "expires_at": 1700003600,
                },
            )
        if request.method == "GET" and path == "/files/file-api-mock123456":
            return httpx.Response(200, json={"id": "file-api-mock123456", "bytes": 512})
        if request.method == "DELETE" and path == "/files/file-api-mock123456":
            return httpx.Response(
                200, json={"id": "file-api-mock123456", "object": "file", "deleted": True}
            )
        if request.method == "POST" and path == "/chat/completions":
            call_counts["chat"] += 1
            body = json.loads(request.content.decode("utf-8"))
            msgs = body["messages"]
            # Simulate 429 transient error on very first text call to verify retry accounting
            if call_counts["chat"] == 1:
                return httpx.Response(429, text="rate limit")
            # Simulate DeepSeek whitespace-only JSON output failure for structured JSON probe
            if body.get("response_format") == {"type": "json_object"}:
                return httpx.Response(
                    200,
                    json={
                        "model": "deepseek-flash",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "    "},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
                    },
                )
            # Tool call turn 1
            if "tools" in body and all(m.get("role") != "tool" for m in msgs):
                return httpx.Response(
                    200,
                    json={
                        "model": "deepseek-flash",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_abc_1",
                                            "type": "function",
                                            "function": {
                                                "name": "inspect_clip",
                                                "arguments": (
                                                    '{"start_ms":0,"end_ms":2000,'
                                                    '"region_id":"full"}'
                                                ),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                    },
                )
            # Multi-image order reply
            first_content = msgs[0].get("content")
            if isinstance(first_content, list) and len(first_content) == 4:
                reply = "FRAME_SEQ_01, FRAME_SEQ_02, FRAME_SEQ_03"
            else:
                reply = "PING_OK"
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                },
            )
        return httpx.Response(404, text="not found")

    sdk_info = inspect_openai_sdk_tool_image_support(cfg.model_id)
    boundaries = run_offline_boundary_suite(cfg)
    entries = build_initial_capability_entries(
        cfg,
        sdk_tool_img_check=sdk_info,
        offline_boundaries=boundaries,
    )

    with ProviderProbeClient(
        config=cfg,
        ledger=ledger,
        api_key="sk-mock-secret-999",
        transport=httpx.MockTransport(handler),
    ) as client:
        execute_live_probe_suite(
            config=cfg,
            gate_config=gate_cfg,
            client=client,
            entries=entries,
            run_id="mock-run-001",
            enable_files_probe=True,
        )

    assert entries["models_discovery"].status == "runtime_verified"
    assert entries["text_chat_completion"].status == "runtime_verified"
    assert entries["vision_multi_image_order"].status == "runtime_verified"
    assert entries["vision_detail_levels"].status == "runtime_verified"
    assert entries["tool_calls_text_observation"].status == "runtime_verified"
    assert entries["files_api_upload_reuse_expiry_delete"].status == "runtime_verified"
    assert entries["files_api_upload_reuse_expiry_delete"].details["p0_production_allowed"] is False
    assert (
        entries["files_api_upload_reuse_expiry_delete"].details[
            "file_id_reuse_makes_inference_free"
        ]
        is False
    )

    # Whitespace-only JSON fallback must NOT be runtime_verified; must preserve error reason
    json_entry = entries["structured_candidate_json"]
    assert json_entry.status == "documented_supported"
    assert json_entry.verification_passed is False
    assert json_entry.last_error is not None
    assert json_entry.last_error.error_code == "WHITESPACE_ONLY_JSON_FALLBACK"

    # Separate accounting: GET /models (1) + Files upload failed (1) + upload retry (1)
    # + retrieve (1) + delete (1) = 5 provider_http_attempts!
    assert ledger.provider_http_attempts == 5
    # Model attempts: text (2 incl 429 retry) + order (1) + detail low/high (2)
    # + json (1) + tool turn1/turn2 (2) + files reuse chat (1) = 9 model_attempts!
    assert ledger.model_attempts == 9
    assert ledger.transferred_bytes > 0


def test_files_api_blocked_without_separate_upload_authorization() -> None:
    cfg = _load_provider_config()
    budget = ProbeBudgetConfig(
        max_model_attempts=4,
        max_provider_http_attempts=4,
        max_transferred_bytes=1_000_000,
        deadline_seconds=60.0,
    )
    gate_cfg = ProbeGateConfig(
        authorization=ProbeAuthorizationConfig(
            cloud_egress_authorized=True,
            live_enabled=True,
            files_api_upload_authorized=False,
            files_api_ttl_seconds=3600,
        ),
        budget=budget,
    )
    ledger = ProbeBudgetLedger(budget=budget)
    entry = CapabilityEntry(
        capability_id="files_api_upload_reuse_expiry_delete",
        description="Files API",
        status="documented_supported",
        documented_status="documented_supported",
        doc_source=cfg.official_sources["files_api"],
    )
    sample = make_synthetic_boundary_image(64, 64, label="tiny", detail="low")

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Should not send HTTP without Files authorization: {request.url}")

    with (
        ProviderProbeClient(
            config=cfg,
            ledger=ledger,
            api_key="sk-mock-secret",
            transport=httpx.MockTransport(fail_if_called),
        ) as client,
        pytest.raises(ProbeGateError) as exc_info,
    ):
        run_files_api_lifecycle_probe(
            client=client,
            gate_config=gate_cfg,
            model_id=cfg.model_id,
            sample_image=sample,
            entry=entry,
            run_id="mock-unauth",
        )
    assert exc_info.value.code == "FILES_API_UNAUTHORIZED"
    assert ledger.provider_http_attempts == 0


def test_network_timeout_marks_cost_status_unknown_and_retains_attempt() -> None:
    cfg = _load_provider_config()
    budget = ProbeBudgetConfig(
        max_model_attempts=2,
        max_provider_http_attempts=2,
        max_transferred_bytes=100_000,
        deadline_seconds=30.0,
    )
    ledger = ProbeBudgetLedger(budget=budget)

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    with ProviderProbeClient(
        config=cfg,
        ledger=ledger,
        api_key="sk-mock-timeout",
        transport=httpx.MockTransport(timeout_handler),
    ) as client:
        res: dict[str, Any] = client.request_model_json(
            operation="probe.timeout_case",
            payload={"model": cfg.model_id, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert res["ok"] is False
    assert "NETWORK_OR_TIMEOUT_UNKNOWN" in str(res["error_code"])
    assert ledger.model_attempts == 1
    assert ledger.transferred_bytes > 0
    assert ledger.cost_status == "unknown"
