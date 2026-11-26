"""Server-owned image preparation in post-rotation display coordinates."""

import hashlib
import io
import re
from contextlib import closing
from pathlib import Path
from typing import Literal

from PIL import Image

from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.application.sampling import SceneProfile, image_size
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import CropBox, FrameEvidence, RegionId, StrictModel
from video_agent.domain.ports import ArtifactStore, ImageInput


class PreparedFrame(StrictModel):
    parent: FrameEvidence
    frame: FrameEvidence
    image: ImageInput
    sha256: str


class CatalogImages:
    def __init__(self, db_path: Path, store: ArtifactStore) -> None:
        self._db = db_path
        self._store = store

    def read(self, asset_id: str, artifact_id: str, *, parent: bool = False) -> bytes:
        with closing(connect_sqlite(self._db)) as conn:
            row = conn.execute(
                "SELECT kind,size_bytes,sha256 FROM artifacts WHERE artifact_id=? AND asset_id=?",
                (artifact_id, asset_id),
            ).fetchone()
        if row is None or row[0] not in (("frame",) if parent else ("frame", "crop")):
            raise MediaError(ErrorCode.CROSS_ASSET_REFERENCE, "unbound image artifact")
        if not 0 < row[1] <= 32 * 1024**2:
            raise MediaError(ErrorCode.CAPABILITY_MISMATCH, "encoded image exceeds byte limit")
        raw = self._store.read_bytes(artifact_id)
        if len(raw) != row[1] or hashlib.sha256(raw).hexdigest() != row[2]:
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "image digest differs from catalog")
        return raw

    def prepare_frame(
        self,
        frame: FrameEvidence,
        profile: SceneProfile,
        region_id: RegionId,
        *,
        verified_high_detail: bool,
    ) -> PreparedFrame:
        if frame.crop is not None or not re.fullmatch(r"full|r[1-3]c[1-3]", region_id):
            raise ValueError("expected a full parent frame and a named grid region")
        raw = self.read(frame.asset_id, frame.artifact_id, parent=True)
        with Image.open(io.BytesIO(raw)) as pixels:
            width, height = pixels.size
            if max(width, height) > 8192 or pixels.format not in {"JPEG", "PNG", "WEBP"}:
                raise MediaError(ErrorCode.CAPABILITY_MISMATCH, "unsupported parent image")
            mime: Literal["image/jpeg", "image/png", "image/webp"] = (
                "image/jpeg"
                if pixels.format == "JPEG"
                else "image/png"
                if pixels.format == "PNG"
                else "image/webp"
            )
            box: CropBox | None = None
            target: Image.Image = pixels
            if region_id != "full":
                r, c = int(region_id[1]) - 1, int(region_id[3]) - 1
                x = max(0, round((c - 0.1) * width / 3))
                y = max(0, round((r - 0.1) * height / 3))
                right = min(width, max(x + 1, round((c + 1.1) * width / 3)))
                bottom = min(height, max(y + 1, round((r + 1.1) * height / 3)))
                box = CropBox(region_id=region_id, x=x, y=y, width=right - x, height=bottom - y)
                target = pixels.crop((x, y, right, bottom))
            size = image_size(*target.size, profile)
            prepared = frame
            if box or size != pixels.size:
                if size != target.size:
                    target = target.resize(size, Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                target.convert("RGB").save(buffer, format="JPEG", quality=92)
                raw = buffer.getvalue()
                aid = self._store.put_bytes(
                    frame.asset_id, "crop" if box else "frame", raw, suffix=".jpg"
                )
                prepared = frame.model_copy(
                    update={"evidence_id": "ev_" + aid, "artifact_id": aid, "crop": box}
                )
                mime = "image/jpeg"
        image = ImageInput(
            evidence_id=prepared.evidence_id,
            artifact_id=prepared.artifact_id,
            actual_pts_ms=prepared.actual_pts_ms,
            width=size[0],
            height=size[1],
            mime_type=mime,
            detail="high" if verified_high_detail else "auto",
        )
        return PreparedFrame(
            parent=frame, frame=prepared, image=image, sha256=hashlib.sha256(raw).hexdigest()
        )
