"""Exercise M2 through the public CLI and the composition used by the batch evaluator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import wave
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from tests.fixtures.p0_media import build_p0_suite
from video_agent.bootstrap import build_slice_container
from video_agent.cli import main
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import PlaybackTimelineMap, TimeRangeMs


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("m2-cli-inputs")
    entries = build_p0_suite(folder, folder / "checked.json")["fixtures"]
    assert len(entries) == 8 and all(row["passed"] for row in entries.values())
    return {key: folder / row["file_path"] for key, row in entries.items()}


@pytest.mark.parametrize("fixture", ["F05", "F04"])
def test_offline_slice_cli_writes_registered_source_evidence_and_playback_map(
    tmp_path: Path, sources: dict[str, Path], capsys: pytest.CaptureFixture[str], fixture: str
) -> None:
    root = tmp_path / "managed"
    assert (
        main(
            [
                "slice",
                "--offline",
                "--video",
                str(sources[fixture]),
                "--question",
                "画面中有什么？",
                "--data-root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)
    trace = json.loads((root / cli_result["evidence_manifest"]).read_text(encoding="utf-8"))
    asset_id = cli_result["asset_id"]
    assert trace["asset_id"] == asset_id and trace["analysis_id"] == cli_result["analysis_id"]
    assert trace["provider_mode"] == "offline_fake" and trace["frames"]
    paths = trace["artifact_paths"]
    if fixture == "F05":
        frame = trace["frames"][0]
        assert frame["rotation_deg"] == 90
        with Image.open(root / paths[frame["artifact_id"]]) as image:
            assert image.size == (96, 160)
            r, g, b = cast(tuple[int, int, int], image.getpixel((16, 143)))
            assert r > 180 and max(g, b) < 70
        assert cli_result["audio_status"] == "no_audio"
    else:
        assert (
            cli_result["audio_status"] == "ok"
            and 170 <= trace["audio_timeline"]["audio_offset_ms"] <= 230
        )
        with wave.open(str(root / paths[trace["audio_artifact_id"]]), "rb") as reader:
            assert reader.getframerate() == 16000 and 0 < reader.getnframes() <= 3 * 16000

    assert "playback_timeline_map" in trace
    playback = PlaybackTimelineMap.model_validate(trace["playback_timeline_map"])
    assert playback.status == "preview_only" and playback.offset_ms is None
    assert (playback.asset_id, playback.artifact_id) == (asset_id, trace["source_artifact_id"])
    source_copy = root / paths[trace["source_artifact_id"]]
    with sources[fixture].open("rb") as source, source_copy.open("rb") as managed:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        assert digest == trace["sha256"] == hashlib.file_digest(managed, "sha256").hexdigest()
    assert source_copy.resolve().is_relative_to(root.resolve())
    catalog = root / "agent.sqlite3"
    assert catalog.is_file()
    with closing(sqlite3.connect(catalog)) as db:
        row = db.execute(
            "SELECT sha256, source_artifact_id FROM assets WHERE asset_id=? AND status='ready'",
            (asset_id,),
        ).fetchone()
        assert row == (digest, trace["source_artifact_id"])
        exports = db.execute(
            "SELECT relative_path FROM artifacts WHERE asset_id=? AND kind='export'", (asset_id,)
        ).fetchall()
    receipts = [json.loads((root / rel).read_text(encoding="utf-8")) for (rel,) in exports]
    assert any(
        r.get("authorization") and r.get("sha256") == digest and r.get("asset_id") == asset_id
        for r in receipts
    )
    assert any(root.rglob("*.cache"))


def test_batch_entrypoint_reuses_source_and_cached_artifacts_across_containers(
    tmp_path: Path, sources: dict[str, Path]
) -> None:
    root = tmp_path / "batch"
    window = TimeRangeMs(start_ms=0, end_ms=500)

    def open_batch() -> tuple[str, str, str]:
        backend = build_slice_container(mode="offline", data_root=root).media_backend
        asset = backend.probe_video(str(sources["F04"]), asset_id="asset_fixed")
        assert asset.source_artifact_id
        (frame,) = backend.extract_frames(asset, [0], range_ms=window)
        audio, timeline = backend.extract_audio(asset)
        assert frame.asset_id == asset.asset_id and 170 <= timeline.audio_offset_ms <= 230
        return asset.source_artifact_id, frame.artifact_id, audio

    first = open_batch()
    assert open_batch() == first
    backend = build_slice_container(mode="offline", data_root=root).media_backend
    with pytest.raises(VideoAgentError) as changed:
        backend.probe_video(str(sources["F05"]), asset_id="asset_fixed")
    assert changed.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert open_batch() == first
