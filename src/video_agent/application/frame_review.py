"""Review real frames through one server-bound QA request."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from video_agent.application.evidence_service import FrameSelection, ToolResult
from video_agent.application.observations import WindowProvenance, WindowResult, fuse_window
from video_agent.application.prompt_registry import PromptRegistry, canonical_json_sha256
from video_agent.application.vision_validation import WindowInputs
from video_agent.domain.errors import DomainValidationError, ErrorCode, MediaError
from video_agent.domain.models import FrameEvidence, Question, RegionId, TimeRangeMs, VideoAsset
from video_agent.domain.ports import ImageInput, MediaBackend, VisionRequest, VisionResponse


@dataclass
class FrameReview:
    asset: VideoAsset
    question: Question
    job_id: str
    media: MediaBackend
    prompts: PromptRegistry
    prepare: Callable[[FrameEvidence, RegionId], tuple[FrameEvidence, ImageInput, str]]
    send: Callable[[VisionRequest, str], Awaitable[VisionResponse]]
    save: Callable[[WindowResult], None]
    remaining: Callable[[], float]

    async def run(self, selection: FrameSelection, parent: str) -> ToolResult:
        self.remaining()
        if self.asset.asset_id != self.question.asset_id:
            raise DomainValidationError(ErrorCode.CROSS_ASSET_REFERENCE, "wrong review asset")
        selection.window.validate_within(self.asset.duration_ms)
        parents = self._frames(selection)
        if selection.compare and len(parents) != 2:
            raise MediaError(ErrorCode.NO_FRAME_IN_RANGE, "two distinct actual frames required")
        if not parents:
            return ToolResult(gaps=[selection.window])
        prepared = [self.prepare(frame, selection.region) for frame in parents]
        frames = [frame for frame, _, _ in prepared]
        images = [image for _, image, _ in prepared]
        for original, frame, image in zip(parents, frames, images, strict=True):
            if (
                frame.asset_id != self.asset.asset_id
                or frame.actual_pts_ms != original.actual_pts_ms
                or frame.evidence_id != image.evidence_id
                or frame.artifact_id != image.artifact_id
                or frame.actual_pts_ms != image.actual_pts_ms
                or (frame.crop.region_id if frame.crop else "full") != selection.region
            ):
                raise DomainValidationError(ErrorCode.INVALID_CITATION, "prepared frame differs")
        inputs = WindowInputs(
            asset_id=self.asset.asset_id,
            analysis_id=self.question.analysis_id,
            window=selection.window,
            frames=frames,
            transcripts=[],
        )
        manifest = [image.model_dump(exclude={"artifact_id"}) for image in images]
        schema = VisionResponse.model_json_schema()
        event = self.question.mode == "event_localization"
        review_context = {
            "operation": "compare_frames" if selection.compare else "inspect_clip",
            "question": self.question.query_text,
            "frames": manifest,
        }
        prompt = self.prompts.render(
            "vision_event_state" if event else "vision_observe",
            {
                "window_start_ms": selection.window.start_ms,
                "window_end_ms": selection.window.end_ms,
                "frames_manifest": json.dumps(
                    manifest if event else review_context, ensure_ascii=False
                ),
                "transcript_context": "",
                "question_text": self.question.query_text,
            },
            tool_schema={},
            output_schema=schema,
        )
        request = VisionRequest(
            asset_id=inputs.asset_id,
            analysis_id=inputs.analysis_id,
            job_id=self.job_id,
            prompt_text=prompt.rendered_text,
            prompt_bundle_hash=prompt.bundle_hash,
            images=images,
            timeout_seconds=min(45.0, self.remaining()),
        )
        response = await self.send(request, parent)
        self.remaining()
        provenance = WindowProvenance(
            provider=response.provider,
            model_id=response.model_id,
            model_revision=response.model_id,
            prompt_bundle_hash=prompt.bundle_hash,
            output_schema=canonical_json_sha256(schema),
            sampling=json.dumps({"requested_ms": selection.points, "tool_call_id": parent}),
            profile="qa",
            crop=selection.region,
            input_digest=canonical_json_sha256(
                [prompt.request_input_digest, [v[2] for v in prepared]]
            ),
        )
        result = fuse_window(inputs, response.observations, provenance, originals=parents)
        self.save(result)
        return ToolResult(observations=result.observations, frames=frames, gaps=result.gaps)

    def _frames(self, selection: FrameSelection) -> list[FrameEvidence]:
        span, points = selection.window, selection.points
        if not 1 <= len(points) <= 16 or any(not span.start_ms <= p < span.end_ms for p in points):
            raise DomainValidationError(ErrorCode.INVALID_TIME_RANGE, "invalid review samples")
        if not selection.compare and span.end_ms - span.start_ms > 8000:
            raise DomainValidationError(ErrorCode.INVALID_TIME_RANGE, "review window too long")
        # Decode in one pass on the common inspect path. Recover individual points when a
        # VFR tail has no successor inside the requested half-open interval.
        try:
            if selection.compare:
                # A distant later frame must not stand in for a missing comparison point.
                decoded = []
                for p in points:
                    nearby = TimeRangeMs(start_ms=p, end_ms=min(span.end_ms, p + 500))
                    pair = self.media.extract_frames(self.asset, [p], range_ms=nearby)
                    if len(pair) != 1 or not p <= pair[0].actual_pts_ms < nearby.end_ms:
                        raise MediaError(ErrorCode.NO_FRAME_IN_RANGE, "comparison point uncovered")
                    decoded.extend(pair)
            else:
                decoded = self.media.extract_frames(self.asset, list(points), range_ms=span)
        except MediaError as exc:
            if exc.code != ErrorCode.NO_FRAME_IN_RANGE or selection.compare:
                raise
            decoded = []
            for point in points:
                self.remaining()
                try:
                    decoded.extend(self.media.extract_frames(self.asset, [point], range_ms=span))
                except MediaError as missing:
                    if missing.code != ErrorCode.NO_FRAME_IN_RANGE:
                        raise
        selected: dict[int, FrameEvidence] = {}
        seen_requests: set[int] = set()
        for f in decoded:
            if (
                f.asset_id != self.asset.asset_id
                or f.duration_ms != self.asset.duration_ms
                or f.crop is not None
                or f.requested_ms not in points
                or f.requested_ms in seen_requests
                or not f.requested_ms <= f.actual_pts_ms < span.end_ms
            ):
                raise DomainValidationError(ErrorCode.INVALID_CITATION, "unbound decoded frame")
            seen_requests.add(f.requested_ms)
            if f.actual_pts_ms not in selected:
                # Preserve the immutable physical frame binding if the media cache is rebuilt.
                key = canonical_json_sha256([f.asset_id, f.evidence_id, f.artifact_id])
                selected[f.actual_pts_ms] = f.model_copy(update={"evidence_id": "ev_frame_" + key})
        return list(selected.values())
