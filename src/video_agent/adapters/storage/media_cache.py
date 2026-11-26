"""Atomic filesystem cache records for derived local media."""

from pathlib import Path
from typing import Literal

from video_agent.adapters.media.thin_media import LocalArtifactStore


class FileMediaCache:
    def __init__(self, root_dir: Path, artifacts: LocalArtifactStore) -> None:
        raise NotImplementedError

    def read(self, key: str) -> bytes | None:
        raise NotImplementedError

    def write(self, key: str, payload: bytes) -> None:
        raise NotImplementedError

    def digest(self, artifact_id: str) -> str:
        raise NotImplementedError

    def bind(
        self,
        artifact_id: str,
        sha256: str,
        *,
        from_asset: str,
        to_asset: str,
        kind: Literal["frame", "audio"],
    ) -> str | None:
        raise NotImplementedError


def model_directory_digest(path: Path) -> str:
    raise NotImplementedError
