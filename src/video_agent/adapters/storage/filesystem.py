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
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.adapters.storage.worker_lock import WorkerLock
from video_agent.application.ingest import StagedSource
from video_agent.domain.errors import ErrorCode, MediaError, StorageError
from video_agent.domain.models import VideoAsset


def _checked_path(path: Path) -> Path:
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
        self.root = _checked_path(root)
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

    def _copy(self, source: Path, dest: Path, initial: os.stat_result) -> StagedSource:
        digest = hashlib.sha256()
        total = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(source, flags), "rb") as reader, dest.open("xb") as writer:
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
                or _identity(_checked_path(source).stat()) != _identity(initial)
            ):
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "source changed during import")
            writer.flush()
            os.fsync(writer.fileno())
        return StagedSource(dest, "art_source_video_" + uuid.uuid4().hex, digest.hexdigest(), total)

    @contextmanager
    def stage(self, path: Path, asset_id: str) -> Iterator[StagedSource]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", asset_id):
            raise StorageError(ErrorCode.STORAGE_ERROR, "invalid managed asset identifier")
        try:
            source = _checked_path(path)
            original = source.stat()
            if not stat.S_ISREG(original.st_mode) or not original.st_size:
                raise MediaError(ErrorCode.CORRUPT_MEDIA, "expected a nonempty regular file")
            if original.st_size > self.max_bytes:
                raise MediaError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "video exceeds import size limit")
            self.root.mkdir(parents=True, exist_ok=True)
            _checked_path(self.root / "worker.lock")
            _checked_path(self.artifacts / asset_id)
            if not _checked_path(self.db_path).exists():
                run_migrations(self.db_path, data_root=self.root, writers_stopped=True)
            with WorkerLock(self.root):
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
                        _checked_path(old).unlink()
                self._check_space(original.st_size)
                temp = self.artifacts / f".import-{uuid.uuid4().hex}.part"
                try:
                    staged = self._copy(source, temp, original)
                    yield staged
                finally:
                    _checked_path(temp).unlink(missing_ok=True)
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
        directory = _checked_path(self.artifacts / asset.asset_id)
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
                    _checked_path(path).unlink(missing_ok=True)
                raise

    def duration_for_asset(self, asset_id: str) -> int:
        with closing(connect_sqlite(self.db_path)) as conn:
            row = conn.execute(
                "SELECT duration_ms FROM assets WHERE asset_id=? AND status='ready'", (asset_id,)
            ).fetchone()
        if row is None:
            raise StorageError(ErrorCode.STORAGE_ERROR, "registered source is missing")
        return int(row[0])
