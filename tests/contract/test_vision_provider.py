import pytest

from video_agent.application.vision_validation import WindowInputs, validate_window
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import (
    AudioTimelineMap,
    FrameEvidence,
    Observation,
    RationalTime,
    TimeRangeMs,
    TranscriptSegment,
)


def inputs_for(asset: str = "asset", analysis: str = "a") -> WindowInputs:
    tb = RationalTime(numerator=1, denominator=1000)
    frame = FrameEvidence(
        asset_id=asset,
        evidence_id="frame",
        artifact_id="image",
        requested_ms=10,
        actual_pts_ms=13,
        raw_pts=13,
        time_base=tb,
        origin_pts=0,
        origin_time_base=tb,
        duration_ms=100,
    )
    speech = TranscriptSegment(
        asset_id=asset,
        evidence_id="speech",
        text="heard words",
        time_range=TimeRangeMs(start_ms=1, end_ms=14),
        asr_version="v",
        audio_timeline=AudioTimelineMap(
            origin_pts=0, origin_time_base=tb, audio_offset_ms=0, extracted_start_ms=0
        ),
    )
    return WindowInputs(
        asset_id=asset,
        analysis_id=analysis,
        window=TimeRangeMs(start_ms=0, end_ms=20),
        frames=[frame],
        transcripts=[speech],
    )


def candidate() -> Observation:
    return Observation(
        asset_id="asset",
        analysis_id="a",
        observation_id="proposed",
        kind="observed",
        text="visible subtitle",
        time_range=TimeRangeMs(start_ms=13, end_ms=14),
        evidence_ids=["frame"],
    )


@pytest.mark.parametrize(
    "change",
    [
        {"analysis_id": "previous"},
        {"asset_id": "other"},
        {"evidence_ids": ["made-up"]},
        {"evidence_ids": ["speech"]},
        {"kind": "heard"},
        {"region_hint": "r1c2", "kind": "heard", "evidence_ids": ["speech"]},
        {"time_range": TimeRangeMs(start_ms=10, end_ms=11)},
        {"time_range": TimeRangeMs(start_ms=13, end_ms=21)},
    ],
)
def test_window_rejects_wrong_owner_modality_and_time(change: dict[str, object]) -> None:
    with pytest.raises(VideoAgentError):
        validate_window(inputs_for(), [candidate().model_copy(update=change)])


def test_valid_inputs_and_claims_must_share_the_window() -> None:
    inputs = inputs_for()
    validate_window(
        inputs,
        [
            candidate(),
            candidate().model_copy(
                update={"kind": "heard", "evidence_ids": ["speech"], "observation_id": "heard"}
            ),
        ],
    )
    with pytest.raises(VideoAgentError):
        validate_window(
            inputs.model_copy(update={"frames": inputs_for("other").frames}), [candidate()]
        )
