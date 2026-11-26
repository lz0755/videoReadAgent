"""Atomic filesystem cache records for derived local media."""

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Literal

from video_agent.adapters.media.thin_media import LocalArtifactStore, _sanitize_segment
from video_agent.domain.errors import ErrorCode, MediaError, StorageError

_MAX_RECORD_BYTES = 16 * 1024 * 1024


def _checksum(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class FileMediaCache:
    def __init__(self, root_dir: Path, artifacts: LocalArtifactStore) -> None:
        self._root = root_dir.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._artifacts = artifacts

    def _path(self, key: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid cache key")
        return self._root / (key + ".cache")

    def read(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            if path.is_symlink():
                return None
            with path.open("rb") as handle:
                checksum = handle.read(32)
                if os.fstat(handle.fileno()).st_size > _MAX_RECORD_BYTES + 32:
                    return None
                payload = handle.read(_MAX_RECORD_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(ErrorCode.STORAGE_ERROR, "cannot read local cache record") from exc
        if (
            len(payload) > _MAX_RECORD_BYTES
            or hashlib.sha256(bytes.fromhex(key) + payload).digest() != checksum
        ):
            return None
        return payload

    def write(self, key: str, payload: bytes) -> None:
        destination = self._path(key)
        if len(payload) > _MAX_RECORD_BYTES:
            return
        staged: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._root, suffix=".tmp", delete=False) as output:
                staged = Path(output.name)
                output.write(hashlib.sha256(bytes.fromhex(key) + payload).digest())
                output.write(payload)
            os.replace(staged, destination)
        except OSError as exc:
            raise StorageError(
                ErrorCode.STORAGE_ERROR, "cannot publish local cache record"
            ) from exc
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def digest(self, artifact_id: str, *, asset_id: str | None = None) -> str:
        try:
            path = self._artifact_path(artifact_id)
            if asset_id is not None and path.parent != self._artifacts.root_dir / _sanitize_segment(
                asset_id
            ):
                raise MediaError(
                    ErrorCode.CROSS_ASSET_REFERENCE, "artifact belongs to another asset"
                )
            return _checksum(path)
        except OSError as exc:
            raise StorageError(ErrorCode.STORAGE_ERROR, "cannot hash managed artifact") from exc

    def _artifact_path(self, artifact_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9_-]+", artifact_id) is None:
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid managed artifact identifier")
        path = self._artifacts.resolve_path(artifact_id)
        resolved = path.resolve(strict=True)
        if path != resolved or not resolved.is_relative_to(self._artifacts.root_dir):
            raise StorageError(ErrorCode.STORAGE_ERROR, "untrusted managed artifact path")
        return resolved

    def _verified(self, artifact_id: str, sha256: str, owner: str) -> Path | None:
        try:
            path = self._artifact_path(artifact_id)
            if (
                path.parent == self._artifacts.root_dir / _sanitize_segment(owner)
                and _checksum(path) == sha256
            ):
                return path
        except FileNotFoundError, StorageError:
            return None
        except OSError as exc:
            raise StorageError(ErrorCode.STORAGE_ERROR, "cannot verify cached media") from exc
        return None

    def bind(
        self,
        artifact_id: str,
        sha256: str,
        *,
        from_asset: str,
        to_asset: str,
        kind: Literal["frame", "audio"],
    ) -> str | None:
        path = self._verified(artifact_id, sha256, from_asset)
        if path is None:
            return None
        if from_asset == to_asset:
            return artifact_id
        # Each asset owns its public artifact. Repeated hits reuse that asset's verified copy.
        alias_key = hashlib.sha256(f"owner-v1\0{kind}\0{to_asset}\0{sha256}".encode()).hexdigest()
        alias = self.read(alias_key)
        if alias is not None:
            with suppress(UnicodeDecodeError):
                existing = alias.decode("ascii")
                if self._verified(existing, sha256, to_asset):
                    return existing
        bound, checksum, _ = self._artifacts.put_file_copy(to_asset, kind, path)
        if checksum != sha256:
            return None
        self.write(alias_key, bound.encode("ascii"))
        return bound


def model_directory_digest(path: Path) -> str:
    """Identify the local weights, tokenizer and settings by file names and full contents."""
    if not (path / "model.bin").is_file():
        raise MediaError(ErrorCode.DEPENDENCY_MISSING, "local ASR weights are unavailable")
    identity = hashlib.sha256()
    try:
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            name = item.relative_to(path).as_posix().encode("utf-8")
            identity.update(len(name).to_bytes(8))
            identity.update(name)
            identity.update(bytes.fromhex(_checksum(item)))
    except OSError as exc:
        raise MediaError(ErrorCode.STORAGE_ERROR, "cannot fingerprint local ASR weights") from exc
    return identity.hexdigest()
