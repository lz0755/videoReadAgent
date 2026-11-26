"""Grounding regressions on synthetic, time-labelled observations and speech."""

from pathlib import Path

import pytest

from video_agent.application.prompt_registry import PromptRegistry
from video_agent.application.slice import SliceSearchIndex
from video_agent.domain.models import (
    AudioTimelineMap,
    Observation,
    RationalTime,
    TimeRangeMs,
    TranscriptSegment,
)


@pytest.fixture
def index() -> SliceSearchIndex:
    search = SliceSearchIndex()
    timeline = AudioTimelineMap(
        origin_pts=0,
        origin_time_base=RationalTime(numerator=1, denominator=1000),
        audio_offset_ms=0,
        extracted_start_ms=0,
        leading_silence_padded=False,
        vad_filter=False,
    )
    for asset, analysis in (("a", "run"), ("a", "other"), ("b", "run")):
        observations = [
            Observation(
                observation_id=f"obs_{ms}",
                asset_id=asset,
                analysis_id=analysis,
                kind="observed",
                text=label,
                time_range=TimeRangeMs(start_ms=ms, end_ms=ms + 1),
                evidence_ids=[f"{asset}_{analysis}_frame_{ms}"],
            )
            for ms, label in (
                (20, "What does the mechanic announce about the brass valve?"),
                (12000, "Mechanic by a brass valve."),
                (15000, "Mechanic grips brass valve."),
                (18000, "Mechanic raises a brass valve."),
            )
        ]
        search.index_observations(asset_id=asset, analysis_id=analysis, observations=observations)
        search.index_transcripts(
            asset_id=asset,
            analysis_id=analysis,
            segments=[
                TranscriptSegment(
                    evidence_id=f"{asset}_{analysis}_speech_{start}",
                    asset_id=asset,
                    time_range=TimeRangeMs(start_ms=start, end_ms=end),
                    text=words,
                    asr_version="synthetic",
                    audio_timeline=timeline,
                )
                for start, end, words in (
                    (11400, 12300, "Steam stopped."),
                    (14500, 15500, "Blue handle."),
                    (17400, 18500, "Seal replaced."),
                )
            ],
        )
    return search


@pytest.mark.parametrize(
    "locator", ["around 12–18 seconds", "between 12 seconds and 18 seconds", "在12至18秒"]
)
def test_located_range_keeps_pictured_and_spoken_endpoints(
    index: SliceSearchIndex, locator: str
) -> None:
    hits = index.search("a", "run", f"What does the mechanic announce {locator}?", limit=5)
    ids = {eid for hit in hits for eid in hit.evidence_ids}
    assert ids >= {
        "a_run_frame_12000",
        "a_run_frame_18000",
        "a_run_speech_11400",
        "a_run_speech_17400",
    }
    assert all(hit.asset_id == "a" and hit.analysis_id == "run" for hit in hits)
    assert "a_run_frame_20" not in ids
    assert len(hits) == len({hit.doc_id for hit in hits}) <= 5


def test_located_point_can_return_speech_without_word_overlap(index: SliceSearchIndex) -> None:
    hits = index.search("a", "run", "What color was specified at 15.25 seconds?", limit=2)
    assert {eid for hit in hits for eid in hit.evidence_ids} == {
        "a_run_frame_15000",
        "a_run_speech_14500",
    }


def test_location_without_coverage_cannot_retrieve_an_unrelated_time(
    index: SliceSearchIndex,
) -> None:
    assert index.search("a", "run", "What does the mechanic announce near 80 seconds?") == []
    assert (
        index.search(
            "a",
            "run",
            "What does the mechanic announce at 15 seconds?",
            time_range=TimeRangeMs(start_ms=0, end_ms=1000),
        )
        == []
    )


def test_duration_and_unitless_number_remain_ordinary_lexical_queries(
    index: SliceSearchIndex,
) -> None:
    for query in (
        "Does the mechanic announce a wait of 80 seconds?",
        "What does the mechanic announce about 80 brass valves?",
    ):
        hits = index.search("a", "run", query, limit=1)
        assert [hit.source_id for hit in hits] == ["obs_20"]


def test_observation_prompt_requests_layout_states_and_frame_specific_changes() -> None:
    registry = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts/registry.json")
    request = registry.render(
        "vision_observe",
        {
            "window_start_ms": 0,
            "window_end_ms": 19000,
            "frames_manifest": "synthetic frame manifest",
            "transcript_context": "synthetic speech",
        },
        tool_schema={},
        output_schema={},
    ).rendered_text.lower()
    assert "left" in request and "right" in request
    assert "checked" in request and "selected" in request
    assert "position" in request and "subtitle" in request
