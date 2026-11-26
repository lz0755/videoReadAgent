"""Source-preserving window fusion."""

from video_agent.application.prompt_registry import canonical_json_sha256
from video_agent.application.sampling import coverage_gaps
from video_agent.application.vision_validation import WindowInputs, validate_window
from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import FrameEvidence, Observation, StrictModel, TimeRangeMs


class WindowProvenance(StrictModel):
    provider: str
    model_id: str
    model_revision: str
    prompt_bundle_hash: str
    output_schema: str
    sampling: str
    profile: str
    crop: str
    input_digest: str


class WindowResult(StrictModel):
    inputs: WindowInputs
    originals: list[FrameEvidence]
    observations: list[Observation]
    provenance: WindowProvenance
    gaps: list[TimeRangeMs]


def timeline_summary(results: list[WindowResult]) -> dict[str, object]:
    """Select up to six cited extracts across the timeline and report actual visual coverage."""
    first = results[0].inputs
    unique = {o.observation_id: o for r in results for o in r.observations}
    ordered = sorted(
        unique.values(),
        key=lambda o: (o.time_range.start_ms, o.time_range.end_ms, o.observation_id),
    )
    if len(ordered) > 6:
        # Keep every observed/heard/inferred modality represented before filling timeline slots.
        selected = {
            next(i for i, o in enumerate(ordered) if o.kind == kind)
            for kind in {o.kind for o in ordered}
        }
        for i in [round(j * (len(ordered) - 1) / 5) for j in range(6)]:
            if len(selected) < 6:
                selected.add(i)
        ordered = [ordered[i] for i in sorted(selected)]
    span = TimeRangeMs(
        start_ms=min(r.inputs.window.start_ms for r in results),
        end_ms=max(r.inputs.window.end_ms for r in results),
    )
    gaps = coverage_gaps(span, (f.actual_pts_ms for r in results for f in r.inputs.frames))
    return {
        "version": "extractive-timeline-v1",
        "asset_id": first.asset_id,
        "analysis_id": first.analysis_id,
        "observation_count": len(unique),
        "entries": [
            o.model_dump(include={"observation_id", "kind", "text", "evidence_ids", "time_range"})
            for o in ordered
        ],
        "visual_coverage_gaps": [g.model_dump() for g in gaps],
    }


def fuse_window(
    inputs: WindowInputs,
    candidates: list[Observation],
    provenance: WindowProvenance,
    *,
    originals: list[FrameEvidence] | None = None,
) -> WindowResult:
    validate_window(inputs, candidates)
    parents = inputs.frames if originals is None else originals
    if len(parents) != len(inputs.frames):
        raise DomainValidationError(ErrorCode.INVALID_CITATION, "frame parent count differs")
    by_frame = {}
    for frame, parent in zip(inputs.frames, parents, strict=True):
        if (
            parent.asset_id != inputs.asset_id
            or parent.crop is not None
            or (
                frame.raw_pts,
                frame.time_base,
                frame.origin_pts,
                frame.origin_time_base,
                frame.actual_pts_ms,
            )
            != (
                parent.raw_pts,
                parent.time_base,
                parent.origin_pts,
                parent.origin_time_base,
                parent.actual_pts_ms,
            )
        ):
            raise DomainValidationError(ErrorCode.INVALID_CITATION, "parent frame timeline differs")
        by_frame[frame.evidence_id] = parent.evidence_id
    window = inputs.window
    combined = list(candidates)
    for segment in inputs.transcripts:
        if segment.text.strip():
            combined.append(
                Observation(
                    observation_id=segment.evidence_id,
                    asset_id=inputs.asset_id,
                    analysis_id=inputs.analysis_id,
                    kind="heard",
                    text=segment.text,
                    evidence_ids=[segment.evidence_id],
                    time_range=TimeRangeMs(
                        start_ms=max(window.start_ms, segment.time_range.start_ms),
                        end_ms=min(window.end_ms, segment.time_range.end_ms),
                    ),
                )
            )
    rows = {}
    for obs in combined:
        facts = obs.model_dump(exclude={"observation_id", "provenance"})
        source = provenance.model_dump()
        if obs.region_hint is not None:
            parents_for_hint = {by_frame[e] for e in obs.evidence_ids if e in by_frame}
            if len(parents_for_hint) != 1:
                raise DomainValidationError(
                    ErrorCode.INVALID_CITATION, "ambiguous region hint parent"
                )
            source["parent_frame"] = parents_for_hint.pop()
            source["hint_window"] = window.model_dump_json()
        oid = "obs_" + canonical_json_sha256([facts, source])
        rows[oid] = obs.model_copy(update={"observation_id": oid, "provenance": source})
    return WindowResult(
        inputs=inputs,
        originals=parents,
        observations=list(rows.values()),
        provenance=provenance,
        gaps=coverage_gaps(window, (f.actual_pts_ms for f in inputs.frames)),
    )
