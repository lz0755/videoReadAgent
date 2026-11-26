"""Live wiring applies analysis and run deadlines to local ASR independently of HTTP."""

import asyncio
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from eval.slice_runner import load_slice_manifest
from tests.fixtures.slice_dataset import SliceDataset
from tests.unit.test_budget_guard import FakeClock
from video_agent.adapters.asr import faster_whisper
from video_agent.adapters.llm.fake import FakePlannerProvider, FakeVisionProvider
from video_agent.application.slice import SliceQuestionSpec, VerticalSliceResult, run_vertical_slice
from video_agent.bootstrap import SliceContainer, build_slice_container
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import AudioTimelineMap, VideoAsset
from video_agent.domain.ports import Transcriber, TranscribeRequest, TranscribeResponse


class TimerLoop(asyncio.BaseEventLoop):
    """A timer and executor loop with an in-process wakeup, including on Windows."""

    def __init__(self) -> None:
        super().__init__()
        self.wakeup = threading.Event()
        self._selector = SimpleNamespace(select=self._wait)

    def _wait(self, timeout: float | None) -> list[object]:
        self.wakeup.wait(timeout)
        self.wakeup.clear()
        return []

    def _process_events(self, events: list[object]) -> None:
        assert not events

    def _write_to_self(self) -> None:
        self.wakeup.set()


def compose(
    root: Path,
    dataset: SliceDataset,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    analysis_seconds: int,
    transcriber: Transcriber | None = None,
) -> tuple[SliceContainer, FakeVisionProvider, FakePlannerProvider]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-unit-test")
    approval = root / "approved.yaml"
    approval.write_text(
        "live_enabled: true\ncloud_egress_authorized: true\nbudget:\n  deadline_seconds: 900\n",
        encoding="utf-8",
    )
    raw = yaml.safe_load((dataset.root / "config/defaults.yaml").read_text(encoding="utf-8"))
    raw["analysis"]["deadline_seconds"] = analysis_seconds
    config = root / "app.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    weights = root / "double-weights"
    weights.mkdir()
    (weights / "model.bin").write_bytes(b"unit test stand-in")
    vision, planner = FakeVisionProvider(), FakePlannerProvider()
    container = build_slice_container(
        mode="live",
        repo_root=dataset.root,
        data_root=root / "work",
        config_path=config,
        budget_config_path=approval,
        asr_model_path=weights,
        clock=clock,
        vision_provider=vision,
        planner_provider=planner,
        transcriber=transcriber,
    )
    return container, vision, planner


async def analyze(container: SliceContainer, dataset: SliceDataset) -> VerticalSliceResult:
    source = dataset.root / load_slice_manifest(dataset.manifest)[0]["relative_path"]
    return await run_vertical_slice(
        str(source),
        asset_id="video",
        analysis_id="analysis",
        questions=[SliceQuestionSpec(question_id="q", query_text="frame")],
        media_backend=container.media_backend,
        transcriber=container.transcriber,
        vision_provider=container.vision_provider,
        planner_provider=container.planner_provider,
        budget_guard=container.budget_guard,
        prompt_registry=container.prompt_registry,
        config=container.config,
        clock=container.clock,
        provider_mode=container.mode,
        max_frames=1,
    )


def test_live_supervisor_completes_130s_local_asr_with_45s_http_attempts(
    tmp_path: Path, synthetic_slice: SliceDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    container, vision, planner = compose(
        tmp_path, synthetic_slice, clock, monkeypatch, analysis_seconds=600
    )

    # Only the local child boundary is simulated. The real supervisor still establishes and
    # enforces its deadline around a decoded response to a real managed WAV request.
    class Channel:
        def send(self, message: tuple[Path, str | None]) -> None:
            with message[0].open("rb") as stream:
                assert stream.read(4) == b"RIFF"

        def poll(self, timeout: float) -> bool:
            assert timeout > 0
            clock.advance(130)
            return True

        def recv(self) -> faster_whisper._Output:
            return faster_whisper._Output([], "en")

    monkeypatch.setattr(faster_whisper, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(faster_whisper.FasterWhisperTranscriber, "_channel", lambda _: Channel())
    with closing(container):
        result = asyncio.run(analyze(container, synthetic_slice), loop_factory=TimerLoop)
    assert result.audio_status == "no_speech"
    assert [c.timeout_seconds for c in vision.calls] == [45]
    assert [c.timeout_seconds for c in planner.calls] == [45]


@pytest.mark.parametrize(
    ("analysis_seconds", "prior_videos_seconds", "media_seconds", "should_start"),
    [(2, 0, 1.94, True), (3600, 890, 9.94, True), (3600, 890, 10.1, False)],
    ids=["analysis-deadline", "remaining-run-deadline", "expired-before-asr"],
)
def test_local_asr_stops_at_remaining_budget_before_any_http_attempt(
    tmp_path: Path,
    synthetic_slice: SliceDataset,
    monkeypatch: pytest.MonkeyPatch,
    analysis_seconds: int,
    prior_videos_seconds: float,
    media_seconds: float,
    should_start: bool,
) -> None:
    class BlockingASR:
        started = False
        cancelled = False

        async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
            self.started = True
            try:
                await asyncio.Event().wait()
                raise AssertionError("no local inference result is available")
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    asr, clock = BlockingASR(), FakeClock()
    container, vision, planner = compose(
        tmp_path,
        synthetic_slice,
        clock,
        monkeypatch,
        analysis_seconds=analysis_seconds,
        transcriber=asr,
    )
    clock.advance(prior_videos_seconds)
    extract_audio = container.media_backend.extract_audio

    def prepare_wav(asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        result = extract_audio(asset)
        clock.advance(media_seconds)
        return result

    monkeypatch.setattr(container.media_backend, "extract_audio", prepare_wav)
    with closing(container), pytest.raises(VideoAgentError) as caught:
        asyncio.run(
            asyncio.wait_for(analyze(container, synthetic_slice), timeout=2), loop_factory=TimerLoop
        )
    assert caught.value.code is ErrorCode.DEADLINE_EXCEEDED
    assert (asr.started, asr.cancelled) == (should_start, should_start)
    assert not vision.calls and not planner.calls
    assert container.budget_guard.get_ledger("analysis").total_attempts == 0
