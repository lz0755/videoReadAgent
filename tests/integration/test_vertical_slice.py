"""Real-media integration tests for M1.5.2 end-to-end vertical slice.

Uses real tmp_path files, real FFmpeg/PyAV, real PromptRegistry, and real
BudgetGuard. Provider Fakes are injected exclusively at declared LLM/ASR ports.
"""

from __future__ import annotations

import io
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from video_agent.adapters.llm.deepseek import DeepSeekPlannerProvider, DeepSeekVisionProvider
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.application.slice import (
    SliceQuestionSpec,
    SliceSearchIndex,
    run_vertical_slice,
    validate_answer_citations,
)
from video_agent.bootstrap import build_slice_container
from video_agent.cli import main as cli_main
from video_agent.domain.errors import DomainValidationError, ErrorCode, ProviderError
from video_agent.domain.models import (
    Answer,
    AudioTimelineMap,
    Claim,
    Observation,
    RationalTime,
    TimeRangeMs,
    TranscriptSegment,
)
from video_agent.domain.ports import (
    PlannerProvider,
    SearchPort,
    VisionProvider,
)
from video_agent.domain.tokenizer import (
    TOKENIZER_VERSION,
    tokenize_text,
    tokenize_to_fts_string,
    validate_search_query,
)


def _run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Step pure async coroutine without creating Windows loopback sockets."""
    try:
        coro.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    else:
        coro.close()
        raise AssertionError("coroutine suspended unexpectedly under offline driver")


def test_domain_tokenizer_chinese_bigram_and_english_terms() -> None:
    assert TOKENIZER_VERSION == "zh_bigram_en_v1"
    assert tokenize_text("修改孔径") == ["修改", "改孔", "孔径"]
    assert tokenize_text("孔径") == ["孔径"]
    assert tokenize_text("ＡＢＣ 孔径=20 Perspective") == ["abc", "孔径", "20", "perspective"]
    assert tokenize_to_fts_string("修改孔径 20") == "修改 改孔 孔径 20"

    norm, tokens, fallback = validate_search_query("孔径")
    assert norm == "孔径"
    assert tokens == ["孔径"]
    assert fallback is False

    norm_single, tokens_single, fallback_single = validate_search_query("孔")
    assert norm_single == "孔"
    assert tokens_single == []
    assert fallback_single is True

    for invalid_query in ("", "   ", "!!!...", "a" * 300):
        with pytest.raises(DomainValidationError) as exc_info:
            validate_search_query(invalid_query)
        assert exc_info.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED


def test_slice_search_index_chinese_retrieval_and_scope_isolation() -> None:
    index = SliceSearchIndex()
    assert isinstance(index, SearchPort)

    timeline = AudioTimelineMap(
        origin_pts=0,
        origin_time_base=RationalTime(numerator=1, denominator=1000),
        audio_offset_ms=0,
        extracted_start_ms=0,
        leading_silence_padded=False,
        vad_filter=False,
    )
    seg = TranscriptSegment(
        evidence_id="ev_asr_001",
        asset_id="asset_a",
        time_range=TimeRangeMs(start_ms=1000, end_ms=3000),
        text="在这里修改孔径参数为 20。",
        asr_version="fake-asr-v1",
        audio_timeline=timeline,
        language="zh",
    )
    obs = Observation(
        observation_id="obs_001",
        asset_id="asset_a",
        analysis_id="an_1",
        kind="observed",
        text="Auto-Align Layers dialog shows Perspective selected.",
        time_range=TimeRangeMs(start_ms=2000, end_ms=4000),
        evidence_ids=["ev_frame_001"],
    )
    other_obs = Observation(
        observation_id="obs_other",
        asset_id="asset_b",
        analysis_id="an_1",
        kind="observed",
        text="另一个视频也提到孔径 200。",
        time_range=TimeRangeMs(start_ms=0, end_ms=1000),
        evidence_ids=["ev_frame_other"],
    )

    index.index_transcripts(asset_id="asset_a", analysis_id="an_1", segments=[seg])
    index.index_observations(asset_id="asset_a", analysis_id="an_1", observations=[obs])
    index.index_observations(asset_id="asset_b", analysis_id="an_1", observations=[other_obs])

    hits_zh = index.search("asset_a", "an_1", "孔径", limit=5)
    assert len(hits_zh) == 1
    assert hits_zh[0].asset_id == "asset_a"
    assert hits_zh[0].analysis_id == "an_1"
    assert hits_zh[0].evidence_ids == ["ev_asr_001"]
    assert "修改孔径参数为 20" in hits_zh[0].text

    # Single-char CJK literal substring fallback inside scoped asset/analysis.
    hits_single = index.search("asset_a", "an_1", "孔", limit=5)
    assert len(hits_single) == 1
    assert hits_single[0].evidence_ids == ["ev_asr_001"]

    # English term retrieval.
    hits_en = index.search("asset_a", "an_1", "Perspective", limit=5)
    assert len(hits_en) == 1
    assert hits_en[0].evidence_ids == ["ev_frame_001"]

    # Analysis isolation: different analysis_id returns no hits.
    assert index.search("asset_a", "an_other", "孔径", limit=5) == []


def test_end_to_end_vertical_slice_real_media_budget_search_and_abstention(tmp_path: Path) -> None:
    f01_path = tmp_path / "f01_cfr.mp4"
    cmd = generate_f01_cfr(f01_path)
    check = self_check_f01_cfr(f01_path, command=cmd)
    assert check.passed, f"F01 Fixture Self Check failed: {check.failure_reasons}"

    container = build_slice_container(mode="offline", data_root=tmp_path / "slice_data")
    assert container.mode == "offline_fake"

    result = _run_coro(
        run_vertical_slice(
            str(f01_path),
            asset_id="asset_slice_f01",
            analysis_id="analysis_slice_001",
            questions=[
                SliceQuestionSpec(question_id="q_aperture", query_text="孔径"),
                SliceQuestionSpec(question_id="q_missing", query_text="火星表面气压"),
            ],
            media_backend=container.media_backend,
            transcriber=container.transcriber,
            vision_provider=container.vision_provider,
            planner_provider=container.planner_provider,
            budget_guard=container.budget_guard,
            prompt_registry=container.prompt_registry,
            config=container.config,
            clock=container.clock,
            provider_mode=container.mode,
            sample_interval_ms=1000,
            max_frames=4,
        )
    )

    assert result.asset.asset_id == "asset_slice_f01"
    assert result.analysis_id == "analysis_slice_001"
    assert len(result.prompt_bundle_hash) == 64
    assert result.provider_mode == "offline_fake"
    assert len(result.frames) >= 2
    assert len(result.observations) >= 1
    assert result.audio_status == "ok"

    # Real frame artifacts are readable images.
    for frame in result.frames:
        raw_img = container.artifact_store.read_bytes(frame.artifact_id)
        with Image.open(io.BytesIO(raw_img)) as img:
            assert img.width == 320 and img.height == 240

    # Budget accounting must count every vision attempt and image sent.
    assert result.analysis_usage_ledger.total_attempts >= 1
    assert result.analysis_usage_ledger.total_images_sent == len(result.frames)

    by_qid = {qr.question.question_id: qr for qr in result.question_results}

    # Question 1 ("孔径"): retrieved from Chinese transcript -> answered with real evidence_id.
    q1 = by_qid["q_aperture"]
    assert len(q1.search_hits) >= 1
    assert q1.answer.status == "answered"
    assert len(q1.answer.claims) >= 1
    for claim in q1.answer.claims:
        for ev_id in claim.evidence_ids:
            assert ev_id in q1.resolved_evidence
            assert q1.resolved_evidence[ev_id].asset_id == "asset_slice_f01"
    assert q1.qa_usage_ledger.total_attempts == 1

    # Question 2 ("火星表面气压"): no matching evidence -> not_answerable, no claims, explicit gap.
    q2 = by_qid["q_missing"]
    assert q2.search_hits == []
    assert q2.answer.status == "not_answerable"
    assert q2.answer.claims == []
    assert len(q2.answer.gaps) >= 1
    assert q2.qa_usage_ledger.total_attempts == 1


def test_vertical_slice_rejects_fabricated_and_cross_asset_citations(tmp_path: Path) -> None:
    f01_path = tmp_path / "f01_cfr.mp4"
    cmd = generate_f01_cfr(f01_path)
    check = self_check_f01_cfr(f01_path, command=cmd)
    assert check.passed

    # 1. Fabricated citation from VisionProvider must fail with INVALID_CITATION.
    c_bad_vision = build_slice_container(
        mode="offline",
        data_root=tmp_path / "bad_vision",
        vision_provider=FakeVisionProvider(scenarios=("fabricated_citation",)),
    )
    with pytest.raises(DomainValidationError) as vis_exc:
        _run_coro(
            run_vertical_slice(
                str(f01_path),
                asset_id="asset_bad_vis",
                analysis_id="an_bad_vis",
                questions=[SliceQuestionSpec(question_id="q1", query_text="孔径")],
                media_backend=c_bad_vision.media_backend,
                transcriber=c_bad_vision.transcriber,
                vision_provider=c_bad_vision.vision_provider,
                planner_provider=c_bad_vision.planner_provider,
                budget_guard=c_bad_vision.budget_guard,
                prompt_registry=c_bad_vision.prompt_registry,
                config=c_bad_vision.config,
                clock=c_bad_vision.clock,
                provider_mode="offline_fake",
                sample_interval_ms=1000,
                max_frames=3,
            )
        )
    assert vis_exc.value.code is ErrorCode.INVALID_CITATION

    # 2. Fabricated citation from PlannerProvider must fail with INVALID_CITATION.
    c_bad_planner = build_slice_container(
        mode="offline",
        data_root=tmp_path / "bad_planner",
        planner_provider=FakePlannerProvider(scenarios=("fabricated_citation",)),
    )
    with pytest.raises(DomainValidationError) as plan_exc:
        _run_coro(
            run_vertical_slice(
                str(f01_path),
                asset_id="asset_bad_plan",
                analysis_id="an_bad_plan",
                questions=[SliceQuestionSpec(question_id="q1", query_text="孔径")],
                media_backend=c_bad_planner.media_backend,
                transcriber=c_bad_planner.transcriber,
                vision_provider=c_bad_planner.vision_provider,
                planner_provider=c_bad_planner.planner_provider,
                budget_guard=c_bad_planner.budget_guard,
                prompt_registry=c_bad_planner.prompt_registry,
                config=c_bad_planner.config,
                clock=c_bad_planner.clock,
                provider_mode="offline_fake",
                sample_interval_ms=1000,
                max_frames=3,
            )
        )
    assert plan_exc.value.code is ErrorCode.INVALID_CITATION

    # 3. Cross-asset citation must raise CROSS_ASSET_REFERENCE.
    cross_answer = Answer(
        answer_id="ans_cross",
        asset_id="asset_a",
        analysis_id="an_1",
        question_id="q_1",
        status="answered",
        claims=[
            Claim(
                text="Citing evidence from another video.",
                evidence_ids=["ev_from_asset_b"],
                support_kind="observed",
            )
        ],
        gaps=[],
        trace_id="trace_cross",
    )
    with pytest.raises(DomainValidationError) as cross_exc:
        validate_answer_citations(
            cross_answer,
            asset_id="asset_a",
            analysis_id="an_1",
            question_id="q_1",
            valid_evidence_ids={"ev_from_asset_a"},
            other_asset_evidence_ids={"ev_from_asset_b"},
        )
    assert cross_exc.value.code is ErrorCode.CROSS_ASSET_REFERENCE


def test_live_mode_requires_consent_and_credentials_with_no_silent_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # Protocols satisfied by thin DeepSeek adapters without network or API key at import/init.
    offline_container = build_slice_container(mode="offline", data_root=tmp_path / "proto")
    ds_vision = DeepSeekVisionProvider(
        artifact_store=offline_container.artifact_store,
        model_id="deepseek-flash",
        cloud_upload_enabled=False,
    )
    ds_planner = DeepSeekPlannerProvider(
        model_id="deepseek-flash",
        cloud_upload_enabled=False,
    )
    assert isinstance(ds_vision, VisionProvider)
    assert isinstance(ds_planner, PlannerProvider)

    # Live container without explicit cloud egress authorization -> CLOUD_DISABLED.
    with pytest.raises(ProviderError) as disabled_exc:
        build_slice_container(
            mode="live",
            data_root=tmp_path / "live_disabled",
            cloud_egress_authorized=False,
        )
    assert disabled_exc.value.code is ErrorCode.CLOUD_DISABLED

    # Live container with cloud egress authorized but missing API key -> CREDENTIAL_MISSING.
    with pytest.raises(ProviderError) as cred_exc:
        build_slice_container(
            mode="live",
            data_root=tmp_path / "live_nokey",
            cloud_egress_authorized=True,
        )
    assert cred_exc.value.code is ErrorCode.CREDENTIAL_MISSING


def test_cli_slice_command_runs_offline_and_outputs_grounded_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    f01_path = tmp_path / "f01_cli.mp4"
    cmd = generate_f01_cfr(f01_path)
    check = self_check_f01_cfr(f01_path, command=cmd)
    assert check.passed

    exit_code = cli_main(
        [
            "slice",
            "--video",
            str(f01_path),
            "--question",
            "孔径",
            "--question",
            "火星表面气压",
            "--data-root",
            str(tmp_path / "cli_work"),
            "--offline",
            "--json",
        ]
    )
    assert exit_code == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["provider_mode"] == "offline_fake"
    assert len(payload["prompt_bundle_hash"]) == 64
    assert len(payload["frames"]) >= 1
    assert payload["analysis_usage"]["total_attempts"] >= 1
    assert payload["analysis_usage"]["total_images_sent"] == len(payload["frames"])
    assert len(payload["questions"]) == 2
    assert payload["questions"][0]["answer"]["status"] == "answered"
    assert payload["questions"][1]["answer"]["status"] == "not_answerable"
