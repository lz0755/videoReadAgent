"""Build versioned search documents from a bound set of committed windows."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from video_agent.application.budget import BudgetGuard
from video_agent.application.observations import WindowProvenance, WindowResult, fuse_window
from video_agent.application.prompt_registry import PromptRegistry, canonical_json_sha256
from video_agent.application.sampling import SampleWindow, SceneProfile, plan_windows
from video_agent.application.stages import WindowStages, window_key
from video_agent.application.vision_validation import WindowInputs, validate_window
from video_agent.domain.config import AppConfig
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import FrameEvidence, TranscriptSegment, VideoAsset
from video_agent.domain.ports import (
    ImageInput,
    MediaBackend,
    Transcriber,
    TranscribeRequest,
    VisionRequest,
    VisionResponse,
)
from video_agent.domain.tokenizer import tokenize_to_fts_string

DOCUMENT_VERSION = "evidence-doc-v1"


@dataclass(frozen=True)
class SearchDocument:
    source_kind: str
    source_id: str
    original_text: str
    tokens: str
    start_ms: int
    end_ms: int


def documents_for(result: WindowResult) -> list[SearchDocument]:
    rows = []
    for item in result.inputs.transcripts:
        if item.text.strip():
            rows.append(
                SearchDocument(
                    "transcript",
                    item.evidence_id,
                    item.text,
                    tokenize_to_fts_string(item.text),
                    item.time_range.start_ms,
                    item.time_range.end_ms,
                )
            )
    for obs in result.observations:
        rows.append(
            SearchDocument(
                "observation",
                obs.observation_id,
                obs.text,
                tokenize_to_fts_string(obs.text),
                obs.time_range.start_ms,
                obs.time_range.end_ms,
            )
        )
    return rows


class IndexWriter(Protocol):
    def publish(
        self,
        asset_id: str,
        analysis_id: str,
        windows: dict[str, str],
        *,
        document_version: str = DOCUMENT_VERSION,
        partial: bool = False,
    ) -> int: ...


@dataclass
class IndexPipeline:
    config: AppConfig
    guard: BudgetGuard
    media: MediaBackend
    transcriber: Transcriber
    prompts: PromptRegistry
    stages: WindowStages
    writer: IndexWriter
    prepare: Callable[[FrameEvidence, SceneProfile], tuple[FrameEvidence, ImageInput, str]]
    send: Callable[[VisionRequest, str], Awaitable[VisionResponse]]
    admit: Callable[[str, FrameEvidence], bool]
    operation_key: Callable[[str, str, str], str]
    provider: str
    model: str
    revision: str
    mode: str
    high_detail: bool

    async def _speech(
        self, asset: VideoAsset, analysis: str
    ) -> tuple[list[TranscriptSegment], str]:
        if not asset.has_audio:
            return [], "no_audio"
        try:
            audio, mapping = self.media.extract_audio(asset)
        except MediaError as exc:
            if exc.code == ErrorCode.NO_AUDIO_STREAM:
                return [], "no_audio"
            raise
        remaining = self.guard.remaining_seconds(analysis)
        request = TranscribeRequest(
            asset_id=asset.asset_id, audio_artifact_id=audio, audio_timeline=mapping
        )
        if self.mode == "live":
            try:
                async with asyncio.timeout(remaining):
                    response = await self.transcriber.transcribe(request)
            except TimeoutError as exc:
                raise MediaError(
                    ErrorCode.DEADLINE_EXCEEDED, "analysis ASR exceeded deadline"
                ) from exc
        else:
            response = await self.transcriber.transcribe(request)
        self.guard.remaining_seconds(analysis)
        if any(
            s.asset_id != asset.asset_id or s.time_range.end_ms > asset.duration_ms
            for s in response.segments
        ):
            raise MediaError(ErrorCode.INVALID_TIMESTAMP, "invalid transcript source or timeline")
        return response.segments, "ok" if response.segments else "no_speech"

    def _frames(self, asset: VideoAsset, w: SampleWindow, analysis: str) -> list[FrameEvidence]:
        try:
            frames = self.media.extract_frames(asset, list(w.requested_ms), range_ms=w.context)
        except MediaError as exc:
            if exc.code != ErrorCode.NO_FRAME_IN_RANGE:
                raise
            frames = []
            for point in w.requested_ms:
                try:
                    frames.extend(self.media.extract_frames(asset, [point], range_ms=w.context))
                except MediaError as missing:
                    if missing.code != ErrorCode.NO_FRAME_IN_RANGE:
                        raise
        selected = {}
        for f in frames:
            if f.actual_pts_ms not in selected and self.admit(analysis, f):
                selected[f.actual_pts_ms] = f
        return list(selected.values())

    async def run(
        self, asset: VideoAsset, analysis: str, job: str, profile: SceneProfile
    ) -> dict[str, object]:
        self.guard.ensure_started(analysis, "analysis")
        self.guard.remaining_seconds(analysis)
        plan = plan_windows(asset.duration_ms, profile, self.config.analysis)
        transcripts, audio_status = await self._speech(asset, analysis)
        schema = VisionResponse.model_json_schema()
        bundle = self.prompts.compute_bundle_hash(tool_schema={}, output_schema=schema)
        keys = {}
        reused = 0
        partial = audio_status != "ok"
        for w in plan.windows:
            self.guard.remaining_seconds(analysis)
            parents = self._frames(asset, w, analysis)
            segments = [
                s
                for s in transcripts
                if s.time_range.start_ms < w.context.end_ms
                and s.time_range.end_ms > w.context.start_ms
            ]
            item = f"{w.core.start_ms}-{w.core.end_ms}"
            sample = {
                "profile": profile,
                "interval_ms": plan.interval_ms,
                "core": w.core.model_dump(),
                "context": w.context.model_dump(),
                "requested_ms": w.requested_ms,
                "high_detail": self.high_detail,
                "max_images": self.config.analysis.max_images_per_request,
            }
            digest = canonical_json_sha256(
                [[f.model_dump() for f in parents], [s.model_dump() for s in segments]]
            )
            key = window_key(
                content_sha256=asset.sha256,
                model=self.model,
                revision=self.revision,
                prompt_bundle=bundle,
                schema=canonical_json_sha256(schema),
                sampling=sample,
                crop="full",
                input_digest=digest,
            )
            keys[item] = key
            if self.stages.load(asset.asset_id, analysis, item, key) is not None:
                reused += 1
            else:
                prepared = [self.prepare(f, profile) for f in parents]
                inputs = WindowInputs(
                    asset_id=asset.asset_id,
                    analysis_id=analysis,
                    window=w.context,
                    frames=[f for f, _, _ in prepared],
                    transcripts=segments,
                )
                obs = []
                revision = self.revision
                send_key = self.operation_key(analysis, item, key)
                batch_size = self.config.analysis.max_images_per_request
                for offset in range(0, len(prepared), batch_size):
                    batch = prepared[offset : offset + batch_size]
                    images = [image for _, image, _ in batch]
                    context = [s.model_dump_json() for s in segments]
                    rendered = self.prompts.render(
                        "vision_observe",
                        {
                            "window_start_ms": w.context.start_ms,
                            "window_end_ms": w.context.end_ms,
                            "frames_manifest": json.dumps(
                                [i.model_dump() for i in images], ensure_ascii=False
                            ),
                            "transcript_context": "\n".join(context),
                        },
                        tool_schema={},
                        output_schema=schema,
                    )
                    response = await self.send(
                        VisionRequest(
                            asset_id=asset.asset_id,
                            analysis_id=analysis,
                            job_id=job,
                            prompt_text=rendered.rendered_text,
                            prompt_bundle_hash=bundle,
                            images=images,
                            transcript_context=context,
                        ),
                        f"{send_key}/{offset}",
                    )
                    validate_window(
                        inputs.model_copy(update={"frames": [f for f, _, _ in batch]}),
                        response.observations,
                    )
                    obs.extend(response.observations)
                    revision = response.model_id
                provenance = WindowProvenance(
                    provider=self.provider,
                    model_id=self.model,
                    model_revision=revision,
                    prompt_bundle_hash=bundle,
                    output_schema=canonical_json_sha256(schema),
                    sampling=json.dumps(sample, sort_keys=True),
                    profile=profile,
                    crop="full",
                    input_digest=canonical_json_sha256([digest, [sha for _, _, sha in prepared]]),
                )
                result = fuse_window(inputs, obs, provenance, originals=parents)
                self.guard.remaining_seconds(analysis)
                self.stages.commit(item, key, result)
            partial = partial or not parents
        self.guard.remaining_seconds(analysis)
        count = self.writer.publish(asset.asset_id, analysis, keys, partial=partial)
        ledger = self.guard.get_ledger(analysis)
        return {
            "analysis_id": analysis,
            "asset_id": asset.asset_id,
            "job_id": job,
            "index_state": "ready",
            "analysis_status": "partial" if partial else "succeeded",
            "audio_status": audio_status,
            "provider_mode": self.mode,
            "windows": len(keys),
            "windows_reused": reused,
            "indexed_documents": count,
            "provider_attempts": ledger.total_attempts,
            "unique_sampled_frames": self.guard.get_account(
                analysis
            ).consumed.unique_sampled_frames,
            "submitted_image_parts": ledger.total_images_sent,
            "cost_status": ledger.cost_status,
            "estimated_cost_usd": ledger.estimated_cost_usd,
            "confirmed_cost_usd": ledger.confirmed_cost_usd,
        }
