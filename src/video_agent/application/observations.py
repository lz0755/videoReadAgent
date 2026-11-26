"""Source-preserving window fusion."""

from video_agent.application.vision_validation import WindowInputs
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


def fuse_window(inputs: WindowInputs, candidates: list[Observation], provenance: WindowProvenance,
                *, originals: list[FrameEvidence] | None = None) -> WindowResult:
    raise NotImplementedError
