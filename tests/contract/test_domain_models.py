"""Contract tests for M1.2 strict domain schemas and invariants."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from video_agent.domain.config import AppConfig, PriceConfig
from video_agent.domain.errors import ErrorCode
from video_agent.domain.models import (
    Answer,
    AudioTimelineMap,
    BoundaryBracket,
    Claim,
    CropBox,
    EventLocalization,
    FrameEvidence,
    Job,
    Observation,
    PlaybackTimelineMap,
    Question,
    RationalTime,
    TimeRangeMs,
    TranscriptSegment,
    UsageRecord,
    VideoAsset,
    compute_canonical_pts_ms,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_SHA256 = "a" * 64


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [
        (-1, 100),
        (0, 0),
        (500, 500),
        (600, 500),
        (0.0, 100),
        (0, 100.5),
        ("0", 100),
        (0, "100"),
        (True, 100),
        (0, True),
        (math.nan, 100),
        (0, math.inf),
        (-math.inf, 100),
    ],
)
def test_time_range_rejects_invalid_types_and_bounds(start_ms: Any, end_ms: Any) -> None:
    """TimeRangeMs strictly enforces integer ms and half-open [start_ms, end_ms)."""
    with pytest.raises(ValidationError):
        TimeRangeMs(start_ms=start_ms, end_ms=end_ms)


def test_time_range_rejects_unknown_fields() -> None:
    """StrictModel forbids extra fields."""
    with pytest.raises(ValidationError):
        TimeRangeMs(start_ms=0, end_ms=1000, extra_field=1)  # type: ignore[call-arg]


def test_rational_pts_compares_before_floor_and_handles_nonzero_origin() -> None:
    """Exact rational comparison happens before flooring; non-zero origin shifts cleanly."""
    # 999.9 ms is strictly inside [0, 1000) and floors to 999
    assert (
        compute_canonical_pts_ms(
            raw_pts=9999,
            time_base_num=1,
            time_base_den=10_000,
            origin_pts=0,
            origin_time_base_num=1,
            origin_time_base_den=10_000,
            duration_ms=1000,
            range_ms=TimeRangeMs(start_ms=0, end_ms=1000),
        )
        == 999
    )

    # Exact 1000.0 ms is NOT inside half-open [0, 1000) and NOT < duration_ms=1000
    with pytest.raises(ValueError, match="NO_FRAME_IN_RANGE|INVALID_TIMESTAMP"):
        compute_canonical_pts_ms(
            raw_pts=10_000,
            time_base_num=1,
            time_base_den=10_000,
            origin_pts=0,
            origin_time_base_num=1,
            origin_time_base_den=10_000,
            duration_ms=1000,
            range_ms=TimeRangeMs(start_ms=0, end_ms=1000),
        )

    # Non-zero origin: raw 5000, 5040, 5120, 5160 ms with origin 5000 ms -> 0, 40, 120, 160 ms
    canonical = [
        compute_canonical_pts_ms(
            raw_pts=raw,
            time_base_num=1,
            time_base_den=1000,
            origin_pts=5000,
            origin_time_base_num=1,
            origin_time_base_den=1000,
            duration_ms=10_000,
        )
        for raw in (5000, 5040, 5120, 5160)
    ]
    assert canonical == [0, 40, 120, 160]


def test_frame_evidence_invariants_and_no_zero_length_range() -> None:
    """FrameEvidence preserves requested vs actual PTS, validates rational match, forbids range."""
    ev = FrameEvidence(
        evidence_id="ev_frame_001",
        asset_id="asset_001",
        artifact_id="art_frame_001",
        requested_ms=3000,
        actual_pts_ms=3040,
        duration_ms=10_000,
        raw_pts=8040,
        time_base=RationalTime(numerator=1, denominator=1000),
        origin_pts=5000,
        origin_time_base=RationalTime(numerator=1, denominator=1000),
        crop=CropBox(region_id="r1c2", x=10, y=20, width=200, height=150),
    )
    assert ev.schema_version == "1.1"
    assert ev.requested_ms == 3000
    assert ev.actual_pts_ms == 3040

    # Reject fabricated actual_pts_ms that disagrees with raw_pts/origin
    with pytest.raises(ValidationError):
        FrameEvidence(
            evidence_id="ev_bad_pts",
            asset_id="asset_001",
            artifact_id="art_frame_001",
            requested_ms=3000,
            actual_pts_ms=3000,
            duration_ms=10_000,
            raw_pts=8040,
            time_base=RationalTime(numerator=1, denominator=1000),
            origin_pts=5000,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
        )

    # Reject actual_pts_ms == duration_ms
    with pytest.raises(ValidationError):
        FrameEvidence(
            evidence_id="ev_at_end",
            asset_id="asset_001",
            artifact_id="art_frame_001",
            requested_ms=9999,
            actual_pts_ms=10_000,
            duration_ms=10_000,
            raw_pts=10_000,
            time_base=RationalTime(numerator=1, denominator=1000),
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
        )

    # Point evidence must NOT disguise itself as a zero-length range
    with pytest.raises(ValidationError):
        FrameEvidence(
            evidence_id="ev_fake_range",
            asset_id="asset_001",
            artifact_id="art_frame_001",
            requested_ms=1000,
            actual_pts_ms=1000,
            duration_ms=10_000,
            raw_pts=1000,
            time_base=RationalTime(numerator=1, denominator=1000),
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            start_ms=1000,  # type: ignore[call-arg]
            end_ms=1000,
        )


def test_observation_and_question_forbid_run_id_and_separate_modalities() -> None:
    """Models use analysis_id uniformly, reject run_id, and separate observed/heard/inferred."""
    obs = Observation(
        observation_id="obs_001",
        asset_id="asset_001",
        analysis_id="analysis_001",
        kind="observed",
        text="Dialog shows diameter 20 mm",
        time_range=TimeRangeMs(start_ms=1000, end_ms=5000),
        evidence_ids=["ev_frame_001"],
        region_hint="r2c2",
    )
    assert obs.schema_version == "1.1"
    assert obs.kind == "observed"

    with pytest.raises(ValidationError):
        Observation(
            observation_id="obs_002",
            asset_id="asset_001",
            run_id="analysis_001",  # type: ignore[call-arg]
            kind="observed",
            text="Bad run_id field",
            time_range=TimeRangeMs(start_ms=1000, end_ms=5000),
            evidence_ids=["ev_frame_001"],
        )

    with pytest.raises(ValidationError):
        Observation(
            observation_id="obs_003",
            asset_id="asset_001",
            analysis_id="analysis_001",
            kind="guessed",  # type: ignore[arg-type]
            text="Invalid support kind",
            time_range=TimeRangeMs(start_ms=1000, end_ms=5000),
            evidence_ids=["ev_frame_001"],
        )

    q = Question(
        question_id="q_001",
        asset_id="asset_001",
        analysis_id="analysis_001",
        query_text="在哪修改了孔径？",
        mode="qa",
        created_at_utc="2026-11-25T10:00:00Z",
    )
    assert q.schema_version == "1.1"


def test_answer_state_invariants_for_answered_partial_not_answerable() -> None:
    """Answer enforces claim/gap invariants and forbids fabricated claims on not_answerable."""
    valid_claim = Claim(
        text="孔径修改为 20",
        evidence_ids=["ev_frame_001"],
        support_kind="observed",
    )

    answered = Answer(
        answer_id="ans_001",
        asset_id="asset_001",
        analysis_id="analysis_001",
        question_id="q_001",
        status="answered",
        claims=[valid_claim],
        gaps=[],
        trace_id="trace_001",
    )
    assert answered.schema_version == "1.1"

    partial = Answer(
        answer_id="ans_002",
        asset_id="asset_001",
        analysis_id="analysis_001",
        question_id="q_001",
        status="partial",
        claims=[valid_claim],
        gaps=["end frame occluded"],
        trace_id="trace_002",
    )
    assert partial.status == "partial"

    not_answerable = Answer(
        answer_id="ans_003",
        asset_id="asset_001",
        analysis_id="analysis_001",
        question_id="q_001",
        status="not_answerable",
        claims=[],
        gaps=["no frame covers the requested dialog"],
        trace_id="trace_003",
    )
    assert not_answerable.claims == []

    # answered without claims fails
    with pytest.raises(ValidationError, match=ErrorCode.SUPPORTED_CLAIM_REQUIRED):
        Answer(
            answer_id="ans_bad_1",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id="q_001",
            status="answered",
            claims=[],
            gaps=[],
            trace_id="trace_bad_1",
        )

    # partial without gaps fails
    with pytest.raises(ValidationError, match=ErrorCode.GAP_REQUIRED):
        Answer(
            answer_id="ans_bad_2",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id="q_001",
            status="partial",
            claims=[valid_claim],
            gaps=[],
            trace_id="trace_bad_2",
        )

    # not_answerable without gaps fails
    with pytest.raises(ValidationError, match=ErrorCode.GAP_REQUIRED):
        Answer(
            answer_id="ans_bad_3",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id="q_001",
            status="not_answerable",
            claims=[],
            gaps=[],
            trace_id="trace_bad_3",
        )

    # not_answerable MUST NOT fabricate claims
    with pytest.raises(ValidationError):
        Answer(
            answer_id="ans_bad_4",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id="q_001",
            status="not_answerable",
            claims=[valid_claim],
            gaps=["insufficient evidence"],
            trace_id="trace_bad_4",
        )


def test_boundary_bracket_and_event_localization_invariants() -> None:
    """BoundaryBracket validates strict ordering, distinct evidence IDs, and estimate bounds."""
    start = BoundaryBracket(
        lower_ms=1000,
        upper_ms=2000,
        duration_ms=10_000,
        before_evidence_id="ev_1",
        after_evidence_id="ev_2",
        estimate_ms=1500,
    )
    end = BoundaryBracket(
        lower_ms=4000,
        upper_ms=5000,
        duration_ms=10_000,
        before_evidence_id="ev_3",
        after_evidence_id="ev_4",
        estimate_ms=4500,
    )
    loc = EventLocalization(label="edit_diameter", start_bracket=start, end_bracket=end)
    assert loc.start_bracket.estimate_ms < loc.end_bracket.estimate_ms

    # Same evidence ID for before and after fails
    with pytest.raises(ValidationError):
        BoundaryBracket(
            lower_ms=1000,
            upper_ms=2000,
            duration_ms=10_000,
            before_evidence_id="ev_1",
            after_evidence_id="ev_1",
            estimate_ms=1500,
        )

    # Estimate outside [lower_ms, upper_ms] fails
    with pytest.raises(ValidationError):
        BoundaryBracket(
            lower_ms=1000,
            upper_ms=2000,
            duration_ms=10_000,
            before_evidence_id="ev_1",
            after_evidence_id="ev_2",
            estimate_ms=2500,
        )

    # Inverted start/end estimates fail
    with pytest.raises(ValidationError):
        EventLocalization(label="bad_order", start_bracket=end, end_bracket=start)


def test_transcript_asset_job_playback_and_usage_contracts() -> None:
    """TranscriptSegment, VideoAsset, Job, PlaybackTimelineMap, and UsageRecord enforce rules."""
    # P0 forbids vad_filter=True
    with pytest.raises(ValidationError):
        AudioTimelineMap(
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            audio_offset_ms=200,
            extracted_start_ms=0,
            leading_silence_padded=False,
            vad_filter=True,  # type: ignore[arg-type]
        )

    seg = TranscriptSegment(
        evidence_id="ev_asr_001",
        asset_id="asset_001",
        time_range=TimeRangeMs(start_ms=200, end_ms=2200),
        text="接下来修改孔径",
        asr_version="faster-whisper-small",
        audio_timeline=AudioTimelineMap(
            origin_pts=0,
            origin_time_base=RationalTime(numerator=1, denominator=1000),
            audio_offset_ms=200,
            extracted_start_ms=0,
        ),
    )
    assert seg.audio_timeline.audio_offset_ms == 200

    # VideoAsset ready requires source_artifact_id
    with pytest.raises(ValidationError):
        VideoAsset(
            asset_id="asset_001",
            source_artifact_id=None,
            sha256=VALID_SHA256,
            size_bytes=1024,
            duration_ms=60_000,
            width=1920,
            height=1080,
            video_codec="h264",
            has_audio=True,
            audio_codec="aac",
            display_filename="demo.mp4",
            status="ready",
        )

    # Job kind vs question_id coupling
    with pytest.raises(ValidationError):
        Job(
            job_id="job_1",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id="q_001",
            kind="analysis",
        )
    with pytest.raises(ValidationError):
        Job(
            job_id="job_2",
            asset_id="asset_001",
            analysis_id="analysis_001",
            question_id=None,
            kind="qa",
        )

    # PlaybackTimelineMap preview_only cannot carry offset_ms; validated_offset needs >= 3 checks
    preview = PlaybackTimelineMap(
        asset_id="asset_001",
        artifact_id="art_src",
        status="preview_only",
        offset_ms=None,
    )
    assert preview.status == "preview_only"
    with pytest.raises(ValidationError):
        PlaybackTimelineMap.model_validate(
            {
                "asset_id": "asset_001",
                "artifact_id": "art_src",
                "status": "validated_offset",
                "offset_ms": 0,
                "verification_records": [{"canonical_ms": 0, "player_ms": 0}],
            }
        )

    # UsageRecord rejects NaN/Infinity/negative costs and distinguishes cost_status
    rec = UsageRecord(
        attempt_id="att_001",
        job_id="job_1",
        scope_id="analysis_001",
        provider="deepseek",
        model_id="deepseek-flash",
        status="unknown",
        images_sent=8,
        estimated_cost_usd=0.05,
        confirmed_cost_usd=None,
        cost_status="unknown",
    )
    assert rec.cost_status == "unknown"
    for bad_cost in (math.nan, math.inf, -0.01):
        with pytest.raises(ValidationError):
            UsageRecord(
                attempt_id="att_bad",
                job_id="job_1",
                scope_id="analysis_001",
                provider="deepseek",
                model_id="deepseek-flash",
                status="succeeded",
                images_sent=1,
                estimated_cost_usd=bad_cost,
                cost_status="estimated",
            )


def test_strict_config_models_load_defaults_and_reject_violations() -> None:
    """AppConfig and PriceConfig validate real repo YAML and reject invalid overrides."""
    raw_defaults = yaml.safe_load((REPO_ROOT / "config" / "defaults.yaml").read_text("utf-8"))
    raw_prices = yaml.safe_load((REPO_ROOT / "config" / "prices.yaml").read_text("utf-8"))

    cfg = AppConfig.model_validate(raw_defaults)
    prices = PriceConfig.model_validate(raw_prices)
    assert cfg.asr.vad_filter is False
    assert cfg.provider.sdk_max_retries == 0
    assert prices.currency == "USD"

    bad_vad = {**raw_defaults, "asr": {"vad_filter": True}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(bad_vad)

    bad_retries = {**raw_defaults, "provider": {**raw_defaults["provider"], "sdk_max_retries": 2}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(bad_retries)


def test_playback_timeline_map_requires_valid_start_mid_end_records() -> None:
    """M1.2 regression: validated_offset rejects empty dicts and requires start/mid/end checks."""
    base: dict[str, Any] = {
        "asset_id": "asset_001",
        "artifact_id": "art_src",
        "status": "validated_offset",
        "offset_ms": 100,
    }

    # Three empty dicts MUST NOT qualify as validated_offset
    with pytest.raises(ValidationError):
        PlaybackTimelineMap.model_validate(
            {**base, "offset_ms": 0, "verification_records": [{}, {}, {}]}
        )

    # Duplicate positions (missing end) MUST be rejected
    with pytest.raises(ValidationError):
        PlaybackTimelineMap.model_validate(
            {
                **base,
                "verification_records": [
                    {
                        "position": "start",
                        "source_pts_ms": 0,
                        "proxy_pts_ms": 100,
                        "observed_offset_ms": 100,
                    },
                    {
                        "position": "mid",
                        "source_pts_ms": 5000,
                        "proxy_pts_ms": 5100,
                        "observed_offset_ms": 100,
                    },
                    {
                        "position": "mid",
                        "source_pts_ms": 9000,
                        "proxy_pts_ms": 9100,
                        "observed_offset_ms": 100,
                    },
                ],
            }
        )

    # Mismatched observed offset exceeding tolerance MUST be rejected
    with pytest.raises(ValidationError):
        PlaybackTimelineMap.model_validate(
            {
                **base,
                "verification_records": [
                    {
                        "position": "start",
                        "source_pts_ms": 0,
                        "proxy_pts_ms": 100,
                        "observed_offset_ms": 100,
                    },
                    {
                        "position": "mid",
                        "source_pts_ms": 5000,
                        "proxy_pts_ms": 5500,
                        "observed_offset_ms": 500,
                    },
                    {
                        "position": "end",
                        "source_pts_ms": 9000,
                        "proxy_pts_ms": 9100,
                        "observed_offset_ms": 100,
                    },
                ],
            }
        )

    # Non-increasing timestamps across start/mid/end MUST be rejected
    with pytest.raises(ValidationError):
        PlaybackTimelineMap.model_validate(
            {
                **base,
                "verification_records": [
                    {
                        "position": "start",
                        "source_pts_ms": 5000,
                        "proxy_pts_ms": 5100,
                        "observed_offset_ms": 100,
                    },
                    {
                        "position": "mid",
                        "source_pts_ms": 5000,
                        "proxy_pts_ms": 5100,
                        "observed_offset_ms": 100,
                    },
                    {
                        "position": "end",
                        "source_pts_ms": 9000,
                        "proxy_pts_ms": 9100,
                        "observed_offset_ms": 100,
                    },
                ],
            }
        )

    # Valid start/mid/end verification records MUST succeed
    valid_map = PlaybackTimelineMap.model_validate(
        {
            **base,
            "verification_records": [
                {
                    "position": "start",
                    "source_pts_ms": 0,
                    "proxy_pts_ms": 100,
                    "observed_offset_ms": 100,
                },
                {
                    "position": "mid",
                    "source_pts_ms": 5000,
                    "proxy_pts_ms": 5105,
                    "observed_offset_ms": 105,
                },
                {
                    "position": "end",
                    "source_pts_ms": 9500,
                    "proxy_pts_ms": 9598,
                    "observed_offset_ms": 98,
                },
            ],
        }
    )
    assert valid_map.status == "validated_offset"
    assert valid_map.offset_ms == 100
    assert len(valid_map.verification_records) == 3
