"""Server-owned image preparation in post-rotation display coordinates."""

from pathlib import Path

from video_agent.application.sampling import SceneProfile
from video_agent.domain.models import FrameEvidence, RegionId, StrictModel
from video_agent.domain.ports import ArtifactStore, ImageInput


class PreparedFrame(StrictModel):
    parent: FrameEvidence
    frame: FrameEvidence
    image: ImageInput
    sha256: str


class CatalogImages:
    def __init__(self, db_path: Path, store: ArtifactStore) -> None:
        raise NotImplementedError

    def read(self, asset_id: str, artifact_id: str) -> bytes:
        raise NotImplementedError

    def prepare_frame(self, frame: FrameEvidence, profile: SceneProfile, region_id: RegionId,
                      *, verified_high_detail: bool) -> PreparedFrame:
        raise NotImplementedError
