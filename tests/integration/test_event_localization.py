"""Real colored frames drive the QA vision port; the expected times come from a separate decode."""

import io
import json
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import av
import pytest
from PIL import Image

from tests.integration.test_budget_ledger import guard_for
from tests.integration.test_observation_pipeline import provenance
from tests.unit.test_budget_guard import _run_coro
from video_agent.adapters.llm.executor import ProviderExecutor
from video_agent.adapters.llm.vision import StrictVisionProvider, check_images
from video_agent.adapters.media.roi import CatalogImages
from video_agent.adapters.media.thin_media import PyAVMediaBackend
from video_agent.adapters.storage.connection import (
    connect_sqlite,
    create_analysis_with_job,
    create_question_with_job,
)
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.observations import SQLiteObservations
from video_agent.adapters.storage.search import SQLiteSearch
from video_agent.adapters.storage.search_index import SQLiteSearchIndex
from video_agent.adapters.storage.stages import StageCache
from video_agent.agent.executor import ToolExecutor
from video_agent.application.budget import BudgetGuard
from video_agent.application.event_localization import EventLocalizer
from video_agent.application.evidence_service import EvidenceService, FrameSelection, ToolResult
from video_agent.application.frame_review import FrameReview
from video_agent.application.observations import fuse_window
from video_agent.application.prompt_registry import PromptRegistry
from video_agent.application.vision_validation import WindowInputs
from video_agent.bootstrap import build_asset_importer, load_app_config
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import FrameEvidence, Observation, Question, RegionId, TimeRangeMs
from video_agent.domain.policies import build_budget_limits
from video_agent.domain.ports import (
    ImageInput,
    SearchHit,
    ToolCallProposal,
    VisionRequest,
    VisionResponse,
)

QUERY = "中央白色方块"


def lit(pixels: Image.Image) -> bool:
    value = pixels.convert("L").getpixel((48, 32))
    assert isinstance(value, int)
    return value > 200


@dataclass
class Clip:
    path: Path
    states: dict[int, bool]


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Clip:
    path = tmp_path_factory.mktemp("visible-event") / "square.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=96x64:r=7:d=23",
            "-vf",
            "drawbox=x=24:y=16:w=48:h=32:c=white:t=fill:enable='gte(t,4.3)*lt(t,14.8)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=30,
        capture_output=True,
    )
    states = {}
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            assert frame.time_base is not None and frame.pts is not None
            ms = int(frame.pts * frame.time_base * 1000)
            states[ms] = lit(frame.to_image())  # type: ignore[no-untyped-call]
    assert len(states) == 161 and list(states) == sorted(states)
    assert all(state == (4300 <= ms < 14800) for ms, state in states.items())
    assert (
        sum(
            a != b
            for a, b in zip(list(states.values())[:-1], list(states.values())[1:], strict=True)
        )
        == 2
    )
    return Clip(path, states)


def test_fixture_visible_edges_and_fractional_frame_times(clip: Clip) -> None:
    positive = [ms for ms, active in clip.states.items() if active]
    assert min(positive) == 4428 and max(positive) == 14714
    assert [ms for ms in clip.states if 14800 <= ms < 15000] == [14857]


class PixelVision:
    provider_name = "fake"
    model_id = "fake-vision"

    def __init__(self, files: CatalogArtifactStore, fault: str) -> None:
        self.files = files
        self.fault = fault
        self.calls: list[VisionRequest] = []

    async def observe(self, request: VisionRequest) -> VisionResponse:
        self.calls.append(request)
        rows = []
        for i, image in enumerate(request.images):
            with Image.open(io.BytesIO(self.files.read_bytes(image.artifact_id))) as pixels:
                visible = lit(pixels)
            state = "active" if visible else "inactive"
            if self.fault == "unclear":
                state = "unclear"
            elif self.fault == "all_active":
                state = "active"
            span = TimeRangeMs(start_ms=image.actual_pts_ms, end_ms=image.actual_pts_ms + 1)
            rows.append(
                Observation(
                    asset_id=request.asset_id,
                    analysis_id=request.analysis_id,
                    observation_id=str(i),
                    kind="observed",
                    text=state,
                    evidence_ids=[image.evidence_id],
                    time_range=span,
                )
            )
            if self.fault == "conflict":
                rows.append(
                    rows[-1].model_copy(
                        update={
                            "observation_id": f"conflict-{i}",
                            "text": "inactive" if visible else "active",
                        }
                    )
                )
        return VisionResponse(
            observations=rows,
            raw_text=json.dumps({"observations": [r.model_dump() for r in rows]}),
            provider="fake",
            model_id=self.model_id,
            provider_request_id=f"call-{len(self.calls)}",
            input_tokens=5,
            output_tokens=5,
            cost_status="confirmed",
        )


@dataclass
class Rig:
    db: Path
    execute: ToolExecutor
    initial: ToolResult
    locate: EventLocalizer
    vision: PixelVision
    guard: BudgetGuard


def make_rig(
    clip: Clip, root: Path, *, fault: str = "", cap: int = 32, after_ms: int = 18150
) -> Rig:
    cfg = load_app_config()
    db = root / "agent.sqlite3"
    run_migrations(db, data_root=root, writers_stopped=True)
    asset = build_asset_importer(root, cfg).import_file(
        clip.path, asset_id="asset", authorization="synthetic fixture"
    )
    question = Question(
        question_id="q",
        asset_id="asset",
        analysis_id="a",
        query_text=QUERY,
        mode="event_localization",
        created_at_utc="now",
    )
    with closing(connect_sqlite(db)) as conn:
        create_analysis_with_job(
            conn,
            asset_id="asset",
            analysis_id="a",
            job_id="an",
            config_hash="cfg",
            prompt_bundle_hash="prompt",
            created_at="now",
        )
        create_question_with_job(
            conn,
            asset_id="asset",
            analysis_id="a",
            job_id="qa",
            question_id="q",
            query_text=QUERY,
            mode=question.mode,
            created_at="now",
        )
    files = CatalogArtifactStore(root)
    media = PyAVMediaBackend(files)
    span = TimeRangeMs(start_ms=0, end_ms=asset.duration_ms)
    coarse = media.extract_frames(asset, [150, 6150, 12150, after_ms], range_ms=span)
    assert [clip.states[f.actual_pts_ms] for f in coarse] == [False, True, True, False]
    obs = [
        Observation(
            asset_id="asset",
            analysis_id="a",
            observation_id="pos",
            kind="observed",
            time_range=TimeRangeMs(
                start_ms=coarse[1].actual_pts_ms, end_ms=coarse[2].actual_pts_ms + 1
            ),
            text=QUERY,
            evidence_ids=[f.evidence_id for f in coarse[1:3]],
        )
    ]
    for f in [coarse[0], coarse[3]]:
        obs.append(
            Observation(
                asset_id="asset",
                analysis_id="a",
                observation_id=f.evidence_id,
                kind="observed",
                time_range=TimeRangeMs(start_ms=f.actual_pts_ms, end_ms=f.actual_pts_ms + 1),
                text="背景",
                evidence_ids=[f.evidence_id],
            )
        )
    inputs = WindowInputs(
        asset_id="asset", analysis_id="a", window=span, frames=coarse, transcripts=[]
    )
    result = fuse_window(inputs, obs, provenance())
    stages = StageCache(db, files)
    stages.commit("all", "coarse", result)
    SQLiteSearchIndex(db, stages).publish("asset", "a", {"all": "coarse"})
    by_id = {o.observation_id: o for o in result.observations}
    sql = SQLiteSearch(db)
    guard = guard_for(db)
    guard.ensure_started(
        "q",
        "qa",
        limits=build_budget_limits(cfg, "qa").model_copy(update={"max_submitted_image_parts": cap}),
    )
    images = CatalogImages(db, files)
    provider = PixelVision(files, fault)
    outbound = ProviderExecutor(guard, authorize=lambda: None)

    def prepare(frame: FrameEvidence, region: RegionId) -> tuple[FrameEvidence, ImageInput, str]:
        item = images.prepare_frame(frame, "software_tutorial", region, verified_high_detail=False)
        return item.frame, item.image, item.sha256

    async def send(request: VisionRequest, parent: str) -> VisionResponse:
        check_images(images, request, max_images=16)
        return await outbound.observe(
            StrictVisionProvider(provider),
            request,
            scope_id="q",
            operation_key=parent,
            parent_tool_call_id=parent,
        )

    async def review(selection: FrameSelection, parent: str) -> ToolResult:
        events = Path(
            "prompts/registry.json" if selection.compare else "prompts/vision/event_registry.json"
        )
        return await FrameReview(
            asset,
            question,
            "qa",
            media,
            PromptRegistry(events),
            prepare,
            send,
            SQLiteObservations(db).save,
            lambda: guard.remaining_seconds("q"),
        ).run(selection, parent)

    service = EvidenceService(
        question, asset.duration_ms, sql, lambda h: by_id[h.source_id], review, lambda r, w: False
    )
    executor = ToolExecutor(guard, service, cfg.qa)
    initial = _run_coro(
        executor.execute(
            ToolCallProposal(
                tool_call_id="search", tool_name="search_evidence", arguments={"query": QUERY}
            )
        )
    )
    assert len(initial.hits) == 1 and len(initial.hits[0].evidence_ids) == 2
    assert initial.hits[0].time_range.end_ms - initial.hits[0].time_range.start_ms > 5000

    def context(hit: SearchHit) -> list[FrameEvidence]:
        assert hit.doc_id == initial.hits[0].doc_id
        return coarse

    return Rig(
        db,
        executor,
        initial,
        EventLocalizer(question, asset.duration_ms, context, executor.execute),
        provider,
        guard,
    )


def test_real_frames_refine_both_transitions_and_reopen_scoped_evidence(
    clip: Clip, tmp_path: Path
) -> None:
    rig = make_rig(clip, tmp_path)
    answer, exposed = _run_coro(rig.locate.run("qa", rig.initial))
    assert answer.status == "answered" and answer.localization and answer.localization.refined
    start, end = answer.localization.start_bracket, answer.localization.end_bracket
    assert start.upper_ms < end.lower_ms
    assert start.lower_ms < 4428 <= start.upper_ms and end.lower_ms < 14857 <= end.upper_ms
    assert len(rig.vision.calls) == 2 and all(1 < len(r.images) <= 16 for r in rig.vision.calls)
    assert all(
        QUERY in r.prompt_text and "inactive" in r.prompt_text and "unclear" in r.prompt_text
        for r in rig.vision.calls
    )
    frames = {f.evidence_id: f for f in exposed.frames}
    with closing(connect_sqlite(rig.db)) as conn:
        for bracket, before, after in ((start, False, True), (end, True, False)):
            assert bracket.estimate_ms == (bracket.lower_ms + bracket.upper_ms) // 2
            assert bracket.upper_ms - bracket.lower_ms <= 4000
            for eid, ms, active in (
                (bracket.before_evidence_id, bracket.lower_ms, before),
                (bracket.after_evidence_id, bracket.upper_ms, after),
            ):
                assert (
                    frames[eid].actual_pts_ms == ms
                    and clip.states[ms] == active
                    and frames[eid].crop is None
                )
                assert conn.execute(
                    "SELECT e.point_ms FROM evidence e JOIN observation_evidence oe "
                    "USING(asset_id,evidence_id) "
                    "JOIN observations o USING(asset_id,observation_id) WHERE e.evidence_id=? "
                    "AND e.asset_id='asset' AND o.analysis_id='a' LIMIT 1",
                    (eid,),
                ).fetchone() == (ms,)
    assert any(f.requested_ms != f.actual_pts_ms for f in frames.values())
    assert all(f.actual_pts_ms in clip.states for f in frames.values())
    counters = rig.guard.get_account("q").consumed
    assert (counters.tool_calls, counters.expensive_tool_calls, counters.provider_attempts) == (
        3,
        2,
        2,
    )
    ledger = rig.guard.get_ledger("q")
    assert ledger.total_images_sent == sum(len(r.images) for r in rig.vision.calls) <= 32
    assert len({r.parent_tool_call_id for r in ledger.records if r.parent_tool_call_id}) == 2


@pytest.mark.parametrize("fault", ["unclear", "all_active", "conflict"])
def test_visibility_and_conflicting_states_cannot_create_a_bracket(
    clip: Clip, tmp_path: Path, fault: str
) -> None:
    rig = make_rig(clip, tmp_path, fault=fault)
    answer, _ = _run_coro(rig.locate.run("qa", rig.initial))
    assert answer.status == "not_answerable" and answer.gaps and answer.localization is None
    assert rig.vision.calls


@pytest.mark.parametrize(("cap", "after", "expected_calls"), [(32, 22500, 0), (14, 18150, 1)])
def test_uncovered_or_unaffordable_second_boundary_stays_unresolved(
    clip: Clip, tmp_path: Path, cap: int, after: int, expected_calls: int
) -> None:
    rig = make_rig(clip, tmp_path, cap=cap, after_ms=after)
    answer, _ = _run_coro(rig.locate.run("qa", rig.initial))
    assert (
        answer.status in {"partial", "not_answerable"}
        and answer.localization is None
        and answer.gaps
    )
    assert (
        len(rig.vision.calls) == expected_calls
        and rig.guard.get_ledger("q").total_images_sent <= cap
    )


def test_compare_does_not_send_two_copies_of_one_decoded_frame(clip: Clip, tmp_path: Path) -> None:
    rig = make_rig(clip, tmp_path)
    with pytest.raises(VideoAgentError):
        _run_coro(
            rig.execute.execute(
                ToolCallProposal(
                    tool_call_id="pair",
                    tool_name="compare_frames",
                    arguments={"first_ms": 301, "second_ms": 302},
                )
            )
        )
    assert not rig.vision.calls
