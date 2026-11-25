"""Contract tests for M1.3 Fake LLM and Fake ASR adapters across all fault scenarios."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, cast

import pytest

from video_agent.adapters.asr.fake import FakeAsrScenario, FakeTranscriber
from video_agent.adapters.llm.fake import FakeLlmScenario, FakePlannerProvider, FakeVisionProvider
from video_agent.domain.errors import ErrorCode, MediaError, ProviderError
from video_agent.domain.models import AudioTimelineMap, RationalTime
from video_agent.domain.ports import (
    ImageInput,
    PlannerRequest,
    TranscribeRequest,
    VisionRequest,
)


def _run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a pure async coroutine without creating OS loopback sockets on Windows."""
    try:
        coro.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    else:
        coro.close()
        raise AssertionError("fake coroutine suspended unexpectedly without an event loop")


def _sample_vision_request() -> VisionRequest:
    return VisionRequest(
        asset_id="asset_001",
        analysis_id="analysis_001",
        job_id="job_001",
        prompt_text="Inspect these frames.",
        prompt_bundle_hash="b" * 64,
        images=[
            ImageInput(
                evidence_id="ev_frame_001",
                artifact_id="art_001",
                width=1920,
                height=1080,
                actual_pts_ms=1000,
            ),
            ImageInput(
                evidence_id="ev_frame_002",
                artifact_id="art_002",
                width=1920,
                height=1080,
                actual_pts_ms=5000,
            ),
        ],
    )


def _sample_planner_request() -> PlannerRequest:
    return PlannerRequest(
        asset_id="asset_001",
        analysis_id="analysis_001",
        question_id="q_001",
        job_id="job_qa_001",
        prompt_text="Answer the user question.",
        prompt_bundle_hash="b" * 64,
        available_evidence_ids=["ev_frame_001", "ev_frame_002"],
    )


def test_fake_vision_normal_and_fabricated_citation_scenarios() -> None:
    """Normal FakeVision cites only provided evidence IDs; fabricated_citation cites unknown ID."""
    req = _sample_vision_request()

    normal_fake = FakeVisionProvider(scenarios=["normal"])
    resp = _run_coro(normal_fake.observe(req))
    assert len(normal_fake.calls) == 1
    assert len(resp.observations) >= 1
    provided_ids = {img.evidence_id for img in req.images}
    for obs in resp.observations:
        assert obs.asset_id == req.asset_id
        assert obs.analysis_id == req.analysis_id
        assert set(obs.evidence_ids).issubset(provided_ids)

    fab_fake = FakeVisionProvider(scenarios=["fabricated_citation"])
    fab_resp = _run_coro(fab_fake.observe(req))
    assert len(fab_resp.observations) == 1
    assert not set(fab_resp.observations[0].evidence_ids).issubset(provided_ids)


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("schema_error", ErrorCode.SCHEMA_VALIDATION_FAILED),
        ("timeout", ErrorCode.PROVIDER_TIMEOUT),
        ("rate_limit", ErrorCode.RATE_LIMITED),
        ("unknown_result", ErrorCode.EXTERNAL_RESULT_UNKNOWN),
    ],
)
def test_fake_vision_error_scenarios(
    scenario: FakeLlmScenario,
    expected_code: ErrorCode,
) -> None:
    """FakeVisionProvider emits exact normalized ProviderError codes for fault scenarios."""
    fake = FakeVisionProvider(scenarios=[scenario])
    with pytest.raises(ProviderError) as exc_info:
        _run_coro(fake.observe(_sample_vision_request()))
    assert exc_info.value.code is expected_code
    assert len(fake.calls) == 1


def test_fake_vision_multi_step_sequence() -> None:
    """FakeVisionProvider replays multi-step sequences (e.g. rate_limit then normal)."""
    fake = FakeVisionProvider(scenarios=["rate_limit", "normal"])
    req = _sample_vision_request()
    with pytest.raises(ProviderError) as exc_info:
        _run_coro(fake.observe(req))
    assert exc_info.value.code is ErrorCode.RATE_LIMITED

    resp = _run_coro(fake.observe(req))
    assert len(resp.observations) >= 1
    assert len(fake.calls) == 2


def test_fake_vision_rejects_frame_at_or_beyond_duration_ms() -> None:
    """M1.3 regression: normal FakeVisionProvider rejects frames at >= duration_ms."""
    fake = FakeVisionProvider(scenarios=["normal"], duration_ms=1000)

    for bad_pts in (1000, 1250):
        bad_req = VisionRequest(
            asset_id="asset_001",
            analysis_id="analysis_001",
            job_id="job_001",
            prompt_text="Inspect boundary frame.",
            prompt_bundle_hash="b" * 64,
            images=[
                ImageInput(
                    evidence_id="ev_boundary",
                    artifact_id="art_boundary",
                    width=1920,
                    height=1080,
                    actual_pts_ms=bad_pts,
                )
            ],
        )
        with pytest.raises(ProviderError) as exc_info:
            _run_coro(fake.observe(bad_req))
        assert exc_info.value.code is ErrorCode.INVALID_TIME_RANGE

    # Last valid millisecond (999 ms for duration 1000 ms) produces valid [999, 1000)
    ok_req = VisionRequest(
        asset_id="asset_001",
        analysis_id="analysis_001",
        job_id="job_001",
        prompt_text="Inspect last valid frame.",
        prompt_bundle_hash="b" * 64,
        images=[
            ImageInput(
                evidence_id="ev_last_valid",
                artifact_id="art_last_valid",
                width=1920,
                height=1080,
                actual_pts_ms=999,
            )
        ],
    )
    ok_resp = _run_coro(fake.observe(ok_req))
    assert len(ok_resp.observations) == 1
    obs = ok_resp.observations[0]
    assert obs.time_range.start_ms == 999
    assert obs.time_range.end_ms == 1000
    obs.time_range.validate_within(1000)


def test_fake_planner_normal_tools_and_error_scenarios() -> None:
    """FakePlannerProvider supports final_answer, call_tools, fabricated_citation, and faults."""
    req = _sample_planner_request()

    ans_fake = FakePlannerProvider(scenarios=["normal"], mode="final_answer")
    ans_resp = _run_coro(ans_fake.decide(req))
    assert ans_resp.action == "final_answer"
    assert ans_resp.answer is not None
    assert ans_resp.answer.asset_id == req.asset_id
    assert ans_resp.answer.analysis_id == req.analysis_id
    assert ans_resp.answer.question_id == req.question_id
    for claim in ans_resp.answer.claims:
        assert set(claim.evidence_ids).issubset(set(req.available_evidence_ids))

    tool_fake = FakePlannerProvider(scenarios=["normal"], mode="call_tools")
    tool_resp = _run_coro(tool_fake.decide(req))
    assert tool_resp.action == "call_tools"
    assert len(tool_resp.tool_calls) >= 1
    assert tool_resp.tool_calls[0].tool_name in {
        "search_evidence",
        "inspect_clip",
        "compare_frames",
    }

    fab_fake = FakePlannerProvider(scenarios=["fabricated_citation"], mode="final_answer")
    fab_resp = _run_coro(fab_fake.decide(req))
    assert fab_resp.answer is not None
    assert not set(fab_resp.answer.claims[0].evidence_ids).issubset(set(req.available_evidence_ids))

    for scenario, code in (
        ("schema_error", ErrorCode.SCHEMA_VALIDATION_FAILED),
        ("timeout", ErrorCode.PROVIDER_TIMEOUT),
        ("rate_limit", ErrorCode.RATE_LIMITED),
        ("unknown_result", ErrorCode.EXTERNAL_RESULT_UNKNOWN),
    ):
        err_fake = FakePlannerProvider(scenarios=[scenario])  # type: ignore[list-item]
        with pytest.raises(ProviderError) as exc_info:
            _run_coro(err_fake.decide(req))
        assert exc_info.value.code is code


def test_fake_transcriber_preserves_audio_offset_and_distinguishes_failures() -> None:
    """FakeTranscriber preserves audio_offset_ms and distinguishes no_speech vs no_audio/errors."""
    req = TranscribeRequest(
        asset_id="asset_001",
        audio_artifact_id="art_audio_001",
        audio_timeline=AudioTimelineMap(
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            audio_offset_ms=200,
            extracted_start_ms=0,
        ),
    )

    normal = FakeTranscriber(scenario="normal")
    resp = _run_coro(normal.transcribe(req))
    assert len(resp.segments) >= 1
    assert resp.segments[0].time_range.start_ms >= 200
    assert resp.segments[0].audio_timeline.vad_filter is False

    no_speech = FakeTranscriber(scenario="no_speech")
    empty_resp = _run_coro(no_speech.transcribe(req))
    assert empty_resp.segments == []

    expected_errors: list[tuple[FakeAsrScenario, ErrorCode]] = [
        ("no_audio", ErrorCode.NO_AUDIO_STREAM),
        ("corrupt_audio", ErrorCode.CORRUPT_MEDIA),
        ("timeout", ErrorCode.PROVIDER_TIMEOUT),
        ("dependency_missing", ErrorCode.DEPENDENCY_MISSING),
    ]
    for scenario, expected_code in expected_errors:
        failing = FakeTranscriber(scenario=scenario)
        with pytest.raises(MediaError) as exc_info:
            _run_coro(failing.transcribe(req))
        assert exc_info.value.code is expected_code
