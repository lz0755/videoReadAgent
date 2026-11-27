"""Local file import, bounded copying and publication in the M1 asset catalog."""

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.application.ingest import StagedSource
from video_agent.domain.errors import ErrorCode, MediaError, StorageError
from video_agent.domain.models import VideoAsset


def checked_local_path(path: Path) -> Path:
    path = path.absolute()
    if path.drive.startswith("\\\\"):
        raise StorageError(ErrorCode.STORAGE_ERROR, "network paths cannot be imported")
    for part in (*reversed(path.parents), path):
        if part.is_symlink() or part.is_junction():
            raise StorageError(ErrorCode.STORAGE_ERROR, "linked media paths are not allowed")
    return path


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


class ManagedSourceStore:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        chunk_bytes: int,
        quota_bytes: int | None,
        progress: Callable[[int], None] | None,
    ) -> None:
        self.root = checked_local_path(root)
        self.artifacts = self.root / "artifacts"
        self.db_path = self.root / "agent.sqlite3"
        self.max_bytes = min(max_bytes, 524_288_000)
        self.chunk_bytes = min(chunk_bytes, 8_388_608)
        self.quota_bytes = quota_bytes
        self.progress = progress

    def _check_space(self, additional_bytes: int) -> None:
        if additional_bytes > shutil.disk_usage(self.root).free:
            raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "insufficient free disk space")
        if self.quota_bytes is not None:
            used = sum(
                p.stat().st_size
                for p in self.artifacts.rglob("*")
                if not p.is_symlink() and not p.is_junction() and p.is_file()
            )
            if used + additional_bytes > self.quota_bytes:
                raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "managed disk quota exceeded")

    def _copy(self, source: Path | BinaryIO, dest: Path, initial: os.stat_result) -> StagedSource:
        digest = hashlib.sha256()
        total = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        opened = (
            os.fdopen(os.open(source, flags), "rb")
            if isinstance(source, Path)
            else nullcontext(source)
        )
        with opened as reader, dest.open("xb") as writer:
            if _identity(os.fstat(reader.fileno())) != _identity(initial):
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "source changed before copying")
            while chunk := reader.read(self.chunk_bytes):
                total += len(chunk)
                if total > initial.st_size or total > self.max_bytes:
                    raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "source grew during import")
                writer.write(chunk)
                digest.update(chunk)
                if self.progress:
                    self.progress(total)
            if (
                total != initial.st_size
                or _identity(os.fstat(reader.fileno())) != _identity(initial)
                or (
                    isinstance(source, Path)
                    and _identity(checked_local_path(source).stat()) != _identity(initial)
                )
            ):
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "source changed during import")
            writer.flush()
            os.fsync(writer.fileno())
        return StagedSource(dest, "art_source_video_" + uuid.uuid4().hex, digest.hexdigest(), total)

    @contextmanager
    def stage(self, path: Path | BinaryIO, asset_id: str) -> Iterator[StagedSource]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", asset_id):
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid managed asset identifier")
        try:
            source = checked_local_path(path) if isinstance(path, Path) else path
            original = source.stat() if isinstance(source, Path) else os.fstat(source.fileno())
            if not stat.S_ISREG(original.st_mode) or not original.st_size:
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "expected a nonempty regular file")
            if original.st_size > self.max_bytes:
                raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "video exceeds import size limit")
            self.root.mkdir(parents=True, exist_ok=True)
            checked_local_path(self.root / "worker.lock")
            checked_local_path(self.root / "import.lock")
            checked_local_path(self.artifacts / asset_id)
            if not checked_local_path(self.db_path).exists():
                run_migrations(self.db_path, data_root=self.root, writers_stopped=True)
            with WorkerLock(self.root, filename="import.lock"):
                self.artifacts.mkdir(exist_ok=True)
                with closing(connect_sqlite(self.db_path)) as conn:
                    if conn.execute(
                        "SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)
                    ).fetchone():
                        raise StorageError(
                            ErrorCode.IDEMPOTENCY_CONFLICT, "asset already registered"
                        )
                # Only this importer creates these immutable temporary names under the writer lock.
                for old in self.artifacts.glob(".import-*.part"):
                    if re.fullmatch(r"\.import-[a-f0-9]{32}\.part", old.name):
                        checked_local_path(old).unlink()
                self._check_space(original.st_size)
                temp = self.artifacts / f".import-{uuid.uuid4().hex}.part"
                try:
                    staged = self._copy(source, temp, original)
                    yield staged
                finally:
                    checked_local_path(temp).unlink(missing_ok=True)
        except PermissionError as exc:
            raise StorageError(
                ErrorCode.STORAGE_ERROR,
                "local filesystem permission denied",
                details={"reason": "permission_denied"},
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "managed disk is full") from exc
            raise StorageError(ErrorCode.STORAGE_ERROR, "managed import failed") from exc

    def publish(self, staged: StagedSource, asset: VideoAsset, authorization: str) -> None:
        directory = checked_local_path(self.artifacts / asset.asset_id)
        directory.mkdir(exist_ok=True)
        suffix = Path(asset.display_filename).suffix.lower()
        target = directory / (
            staged.artifact_id + (suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin")
        )
        timestamp = datetime.now(UTC).isoformat()
        receipt_id = "art_export_" + uuid.uuid4().hex
        receipt = directory / (receipt_id + ".json")
        payload = json.dumps(
            {
                "asset_id": asset.asset_id,
                "sha256": asset.sha256,
                "authorization": authorization,
                "recorded_at": timestamp,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._check_space(len(payload))
        written: list[Path] = []
        with closing(connect_sqlite(self.db_path)) as conn:
            try:
                with immediate_transaction(conn):
                    conn.execute(
                        "INSERT INTO assets(asset_id,sha256,size_bytes,duration_ms,width,height,"
                        "video_codec,has_audio,audio_codec,display_filename,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,'importing',?)",
                        (
                            asset.asset_id,
                            asset.sha256,
                            asset.size_bytes,
                            asset.duration_ms,
                            asset.width,
                            asset.height,
                            asset.video_codec,
                            int(asset.has_audio),
                            asset.audio_codec,
                            asset.display_filename,
                            timestamp,
                        ),
                    )
                    os.replace(staged.path, target)
                    written.append(target)
                    with receipt.open("xb") as handle:
                        written.append(receipt)
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    for aid, kind, path, digest, size in [
                        (
                            staged.artifact_id,
                            "source_video",
                            target,
                            asset.sha256,
                            asset.size_bytes,
                        ),
                        (
                            receipt_id,
                            "export",
                            receipt,
                            hashlib.sha256(payload).hexdigest(),
                            len(payload),
                        ),
                    ]:
                        conn.execute(
                            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                            (
                                aid,
                                asset.asset_id,
                                kind,
                                path.relative_to(self.root).as_posix(),
                                digest,
                                size,
                                timestamp,
                            ),
                        )
                    conn.execute(
                        "UPDATE assets SET source_artifact_id=?, status='ready' WHERE asset_id=?",
                        (staged.artifact_id, asset.asset_id),
                    )
            except BaseException:
                for path in written:
                    checked_local_path(path).unlink(missing_ok=True)
                raise

    def duration_for_asset(self, asset_id: str) -> int:
        with closing(connect_sqlite(self.db_path)) as conn:
            row = conn.execute(
                "SELECT duration_ms FROM assets WHERE asset_id=? AND status='ready'", (asset_id,)
            ).fetchone()
        if row is None:
            raise StorageError(ErrorCode.STORAGE_ERROR, "registered source is missing")
        return int(row[0])

    def existing(self, source: Path, asset_id: str, *, max_duration_ms: int) -> VideoAsset | None:
        """A batch may revisit its fixed asset ID. Verify both copies before reuse."""
        if not checked_local_path(self.db_path).exists():
            return None
        with closing(connect_sqlite(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT a.*, f.relative_path FROM assets a JOIN artifacts f "
                "ON a.source_artifact_id=f.artifact_id AND a.asset_id=f.asset_id "
                "WHERE a.asset_id=? AND a.status='ready'",
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        if row["size_bytes"] > self.max_bytes or row["duration_ms"] > max_duration_ms:
            raise MediaError(
                ErrorCode.MEDIA_LIMIT_EXCEEDED, "registered video exceeds current limits"
            )
        managed = checked_local_path(self.root / row["relative_path"])
        if managed.parent != self.artifacts / asset_id:
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid registered source location")
        try:
            path = checked_local_path(source)
            st = path.stat()
            if not stat.S_ISREG(st.st_mode) or st.st_size != row["size_bytes"]:
                raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "registered source differs")
            with path.open("rb") as handle:
                if _identity(os.fstat(handle.fileno())) != _identity(st):
                    raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "selected source changed")
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            if checksum != row["sha256"] or _identity(checked_local_path(path).stat()) != _identity(
                st
            ):
                raise StorageError(ErrorCode.IDEMPOTENCY_CONFLICT, "registered source differs")
            with managed.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != checksum:
                    raise MediaError(ErrorCode.CORRUPT_MEDIA, "managed source checksum changed")
        except OSError as exc:
            raise StorageError(ErrorCode.STORAGE_ERROR, "registered source unavailable") from exc
        fields = {key: value for key, value in dict(row).items() if key in VideoAsset.model_fields}
        fields["has_audio"] = bool(fields["has_audio"])
        return VideoAsset.model_validate(fields)


class ManagedArtifactFiles(LocalArtifactStore):
    """Low-level ArtifactStore for opaque inputs, including files supplied before registration."""

    def __init__(self, root: Path) -> None:
        super().__init__(checked_local_path(root / "artifacts"))

    def _safe_target_dir(self, asset_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", asset_id):
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid asset identifier")
        directory = checked_local_path(self.root_dir / asset_id)
        directory.mkdir(exist_ok=True)
        return directory

    def resolve_path(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", artifact_id):
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid artifact identifier")
        path = checked_local_path(super().resolve_path(artifact_id))
        if not path.is_relative_to(self.root_dir):
            raise StorageError(ErrorCode.STORAGE_ERROR, "artifact escaped managed storage")
        return path


class CatalogArtifactStore(ManagedArtifactFiles):
    """Registered media is visible by its committed owner, kind and location."""

    def __init__(self, root: Path) -> None:
        self._catalog = checked_local_path(root / "agent.sqlite3")
        super().__init__(root)

    def _record(self, artifact_id: str) -> tuple[Path, str]:
        with closing(connect_sqlite(self._catalog)) as conn:
            row = conn.execute(
                "SELECT asset_id,kind,relative_path,sha256,size_bytes FROM artifacts "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise StorageError(ErrorCode.STORAGE_ERROR, "artifact is not registered")
        owner, kind, relative, digest, size = row
        path = checked_local_path(self.root_dir.parent / relative)
        prefixes = ("art_export_", "art_slice_evidence_") if kind == "export" else (f"art_{kind}_",)
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner)
            or Path(relative).is_absolute()
            or path.parent != self.root_dir / owner
            or path.stem != artifact_id
            or not artifact_id.startswith(prefixes)
            or not path.is_file()
            or path.stat().st_size != size
        ):
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid registered artifact binding")
        return path, digest

    def resolve_path(self, artifact_id: str) -> Path:
        return self._record(artifact_id)[0]

    def verify_hash(self, artifact_id: str, expected_sha256: str) -> bool:
        path, registered = self._record(artifact_id)
        if registered != expected_sha256:
            return False
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == registered

    def _register(self, asset_id: str, kind: str, artifact_id: str, digest: str, size: int) -> None:
        path = super().resolve_path(artifact_id)
        try:
            with closing(connect_sqlite(self._catalog)) as conn, immediate_transaction(conn):
                conn.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        asset_id,
                        "export" if kind == "slice_evidence" else kind,
                        path.relative_to(self.root_dir.parent).as_posix(),
                        digest,
                        size,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except BaseException as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, sqlite3.Error):
                raise StorageError(
                    ErrorCode.STORAGE_ERROR, "could not register derived artifact"
                ) from exc
            raise

    def put_bytes(self, asset_id: str, kind: str, data: bytes, *, suffix: str) -> str:
        artifact_id = super().put_bytes(asset_id, kind, data, suffix=suffix)
        self._register(asset_id, kind, artifact_id, hashlib.sha256(data).hexdigest(), len(data))
        return artifact_id

    def put_file_copy(self, asset_id: str, kind: str, source_path: Path) -> tuple[str, str, int]:
        copied = super().put_file_copy(asset_id, kind, source_path)
        self._register(asset_id, kind, *copied)
        return copied
