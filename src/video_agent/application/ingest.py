"""Registration of a user-selected local video through managed storage and probe ports."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from video_agent.domain.models import VideoAsset


@dataclass(frozen=True, slots=True)
class StagedSource:
    path: Path
    artifact_id: str
    sha256: str
    size_bytes: int


class ImportStore(Protocol):
    def stage(self, path: Path, asset_id: str) -> AbstractContextManager[StagedSource]: ...

    def publish(self, staged: StagedSource, asset: VideoAsset, authorization: str) -> None: ...


class AssetImporter:
    def __init__(
        self, store: ImportStore, probe: Callable[[StagedSource, str, str], VideoAsset],
        max_duration_ms: int,
    ) -> None:
        pass

    def import_file(self, source_path: Path, *, asset_id: str, authorization: str) -> VideoAsset:
        raise NotImplementedError
