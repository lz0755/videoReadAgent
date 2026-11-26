"""Managed CLI imports through real files, media binaries, and the disk catalog."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from video_agent.bootstrap import build_asset_importer, load_app_config
from video_agent.cli import main
from video_agent.domain.errors import ErrorCode, VideoAgentError

MIB = 1024 * 1024


@pytest.fixture
def movie(tmp_path: Path) -> Path:
    path = tmp_path / "original.mp4"
    generate_f01_cfr(path)
    assert self_check_f01_cfr(path).passed
    return path


def pad_mp4(path: Path, size: int) -> None:
    with path.open("ab") as stream:
        padding = size - stream.tell()
        stream.write(padding.to_bytes(4, "big") + b"free")
        stream.truncate(size)


def catalog(root: Path) -> list[tuple[str, str, str, str]]:
    db = root / "agent.sqlite3"
    if not db.exists():
        return []
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT a.status, f.kind, f.relative_path, f.sha256 FROM assets a "
            "JOIN artifacts f USING(asset_id) ORDER BY kind"
        ).fetchall()


def test_cli_registers_managed_source_and_local_authorization(
    movie: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "managed"
    assert (
        main(
            [
                "assets",
                "import",
                str(movie),
                "--data-root",
                str(root),
                "--json",
                "--authorization",
                "synthetic-fixture-F01",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    digest = hashlib.sha256(movie.read_bytes()).hexdigest()
    assert data["asset_id"] and data["sha256"] == digest
    assert data["duration_ms"] == 3000 and data["status"] == "ready"
    rows = catalog(root)
    assert [(s, k) for s, k, _, _ in rows] == [("ready", "export"), ("ready", "source_video")]
    movie.unlink()
    for _, kind, relative, expected in rows:
        assert not Path(relative).is_absolute()
        payload = (root / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected
        if kind == "export":
            receipt = json.loads(payload)
            assert receipt["authorization"] == "synthetic-fixture-F01"
            assert (receipt["asset_id"], receipt["sha256"]) == (data["asset_id"], digest)
        else:
            assert expected == digest


def test_exact_500_mib_is_copied_in_8_mib_chunks(movie: Path, tmp_path: Path) -> None:
    root = tmp_path / "managed"
    pad_mp4(movie, 500 * MIB)
    result = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(movie)]
    )
    assert {s["codec_type"] for s in json.loads(result)["streams"]} == {"audio", "video"}
    progress: list[int] = []
    importer = build_asset_importer(root, load_app_config(), progress=progress.append)
    asset = importer.import_file(movie, asset_id="limit", authorization="synthetic")
    assert asset.size_bytes == 524_288_000
    assert progress == [*range(8 * MIB, 500 * MIB, 8 * MIB), 500 * MIB]
    with movie.open("rb") as stream:
        assert asset.sha256 == hashlib.file_digest(stream, "sha256").hexdigest()
    assert (root / catalog(root)[-1][2]).stat().st_size == 500 * MIB


@pytest.mark.parametrize(
    "failure", ["limit", "quota", "changed", "interrupt", "corrupt", "duration"]
)
def test_failed_import_cleans_up(movie: Path, tmp_path: Path, failure: str) -> None:
    root = tmp_path / "managed"
    config = load_app_config()
    if failure == "limit":
        pad_mp4(movie, 500 * MIB + 1)
    elif failure in {"changed", "interrupt"}:
        pad_mp4(movie, 9 * MIB)
    elif failure == "corrupt":
        movie.write_bytes(b"invalid media")
    elif failure == "duration":
        config = config.model_copy(
            update={"media": config.media.model_copy(update={"max_duration_ms": 2999})}
        )

    calls: list[int] = []

    def progress(count: int) -> None:
        calls.append(count)
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "changed" and len(calls) == 1:
            with movie.open("ab") as stream:
                stream.write(b"changed")

    importer = build_asset_importer(
        root, config, quota_bytes=128 if failure == "quota" else None, progress=progress
    )
    with pytest.raises((VideoAgentError, KeyboardInterrupt)) as caught:
        importer.import_file(movie, asset_id="failed", authorization="synthetic")
    if failure in {"quota", "limit", "duration"}:
        assert isinstance(caught.value, VideoAgentError)
        assert caught.value.code is ErrorCode.MEDIA_LIMIT_EXCEEDED
    if failure in {"limit", "quota"}:
        assert calls == []
    if failure == "interrupt":
        assert isinstance(caught.value, KeyboardInterrupt)
    assert not catalog(root)
    assert not any(p.is_file() for p in (root / "artifacts").rglob("*"))


def link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def test_import_rejects_linked_source_and_linked_storage(tmp_path: Path, movie: Path) -> None:
    outside = tmp_path / "external"
    outside.mkdir()
    real = movie.rename(outside / "video.mp4")
    link = tmp_path / "link"
    link_directory(link, outside)
    root = tmp_path / "managed"
    importer = build_asset_importer(root, load_app_config())
    with pytest.raises(VideoAgentError):
        importer.import_file(link / real.name, asset_id="via_link", authorization="synthetic")
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    link_directory(root / "artifacts" / "redirect", outside)
    with pytest.raises(VideoAgentError):
        importer.import_file(real, asset_id="redirect", authorization="synthetic")
    assert list(outside.iterdir()) == [real]
    assert not catalog(root)


def test_quota_accounts_for_previous_import_and_duplicate_id_is_safe(
    movie: Path, tmp_path: Path
) -> None:
    size = movie.stat().st_size
    root = tmp_path / "managed"
    importer = build_asset_importer(root, load_app_config(), quota_bytes=size + 32_768)
    first = importer.import_file(movie, asset_id="original", authorization="synthetic")
    rows = catalog(root)
    with pytest.raises(VideoAgentError):
        importer.import_file(movie, asset_id="different", authorization="synthetic")
    existing = build_asset_importer(root, load_app_config())
    with pytest.raises(VideoAgentError) as conflict:
        existing.import_file(movie, asset_id="original", authorization="synthetic")
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert catalog(root) == rows
    assert first.sha256 == hashlib.sha256((root / rows[-1][2]).read_bytes()).hexdigest()
    assert all(
        hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
        for _, _, path, digest in rows
    )
