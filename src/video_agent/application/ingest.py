"""Registration of a user-selected local video through managed storage and probe ports."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import VideoAsset


@dataclass(frozen=True, slots=True)
class StagedSource:
    path: Path
    artifact_id: str
    sha256: str
    size_bytes: int


class ImportStore(Protocol):
    def stage(
        self, path: Path | BinaryIO, asset_id: str
    ) -> AbstractContextManager[StagedSource]: ...

    def publish(self, staged: StagedSource, asset: VideoAsset, authorization: str) -> None: ...


class AssetImporter:
    def __init__(
        self,
        store: ImportStore,
        probe: Callable[[StagedSource, str, str], VideoAsset],
        max_duration_ms: int,
    ) -> None:
        self._store = store
        self._probe = probe
        self._max_duration_ms = max_duration_ms

    def import_file(self, source_path: Path, *, asset_id: str, authorization: str) -> VideoAsset:
        return self.register(
            source_path, source_path.name, asset_id=asset_id, authorization=authorization
        )

    def register(
        self, source: Path | BinaryIO, display_name: str, *, asset_id: str, authorization: str
    ) -> VideoAsset:
        if not authorization.strip():
            raise MediaError(ErrorCode.STORAGE_ERROR, "a local authorization record is required")
        with self._store.stage(source, asset_id) as staged:
            asset = self._probe(staged, asset_id, display_name)
            if asset.duration_ms > self._max_duration_ms:
                raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "video exceeds duration limit")
            self._store.publish(staged, asset, authorization)
        return asset
