import hashlib
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import av
import pytest

from eval.runner import main
from video_agent.adapters.storage.connection import connect_sqlite


@pytest.fixture
def fixture_manifest(tmp_path: Path) -> Path:
    video = tmp_path / "generated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=4:duration=2",
            "-an",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    with av.open(str(video)) as container:
        assert not container.streams.audio
        frames = list(container.decode(video=0))
        assert len(frames) == 8
        assert [
            int(f.pts * f.time_base * 1000)
            for f in frames
            if f.pts is not None and f.time_base is not None
        ] == list(range(0, 2000, 250))
    doc = {
        "fixture": True,
        "media_artifacts": [
            {
                "artifact_id": "fixture",
                "split": "m0_dev",
                "media_kind": "video",
                "relative_path": video.name,
                "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "source_group_id": "s",
                "original_id": "o",
                "benchmark": "generated",
                "authorization": {"usage_scope": "generated engineering fixture"},
            }
        ],
        "regular_qa": [{"question_id": "q1", "video_id": "fixture", "question_text": "observed"}],
        "localization_requests": [
            {
                "request_id": "e1",
                "video_id": "fixture",
                "query_text": "observed",
                "mode": "event_localization",
            }
        ],
    }
    manifest = tmp_path / "fixture.json"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    return manifest


def test_fixture_properties(fixture_manifest: Path) -> None:
    doc = json.loads(fixture_manifest.read_bytes())
    assert len(doc["regular_qa"]) == len(doc["localization_requests"]) == 1
    assert doc["regular_qa"][0]["question_id"] != doc["localization_requests"][0]["request_id"]


def test_offline_executes_both_production_paths_and_preserves_first_report(
    fixture_manifest: Path,
    tmp_path: Path,
) -> None:
    out = tmp_path / "run"
    args = [
        "--manifest",
        str(fixture_manifest),
        "--media-root",
        str(tmp_path),
        "--output-dir",
        str(out),
    ]
    assert main(args) == 0
    report = json.loads((out / "report.json").read_bytes())
    assert report["mode"] == "offline_fake" and report["quality"] is None
    assert report["populations"] == {"qa": 1, "event_localization": 1}
    assert report["usage"]["provider_attempts"] > 0 and report["peak_rss_bytes"] > 0
    assert len(report["provenance"]["lock_sha256"]) == 64
    assert report["provenance"]["models"]["vision"] == "fake-vision"
    assert "observed" not in (out / "report.json").read_text(encoding="utf-8")
    with closing(connect_sqlite(out / "managed" / "agent.sqlite3")) as conn:
        rows = conn.execute("SELECT mode, analysis_id FROM questions ORDER BY mode").fetchall()
        assert [r[0] for r in rows] == ["event_localization", "qa"] and rows[0][1] == rows[1][1]
        assert conn.execute("SELECT count(*) FROM answers").fetchone()[0] == 2
    original = (out / "report.json").read_bytes()
    assert main(args) != 0 and (out / "report.json").read_bytes() == original


@pytest.mark.parametrize("fault", ["budget", "consent", "scope", "old_approval"])
def test_live_refuses_before_touching_output_or_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    out = tmp_path / "live"
    args = [
        "--manifest",
        "eval/manifests/heldout.jsonl",
        "--event-manifest",
        "eval/manifests/heldout_events.jsonl",
        "--output-dir",
        str(out),
        "--live",
    ]
    if fault != "budget":
        budget: dict[str, Any] = {
            "authorization": {"live_enabled": True, "cloud_egress_authorized": fault != "consent"},
            "budget": {
                "max_provider_attempts": 1,
                "max_submitted_image_parts": 4,
                "deadline_seconds": 10,
                "max_estimated_cost_usd": 0.1,
            },
        }
        if fault != "old_approval":
            budget.update(
                purpose="m6_heldout",
                provider="deepseek",
                model="deepseek-flash",
                authorized_media_sha256=[],
                authorization_ref="example",
                output_dir=str(out),
            )
        path = tmp_path / "budget.json"
        path.write_text(json.dumps(budget), encoding="utf-8")
        args += ["--budget", str(path)]
    assert main(args) != 0
    message = capsys.readouterr()
    assert json.loads(message.out)["status"] == "blocked"
    assert not out.exists() and str(tmp_path) not in message.out + message.err


def test_modified_media_is_rejected_before_execution(
    fixture_manifest: Path, tmp_path: Path
) -> None:
    with (tmp_path / "generated.mp4").open("ab") as handle:
        handle.write(b"changed")
    out = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.runner",
            "--manifest",
            str(fixture_manifest),
            "--media-root",
            str(tmp_path),
            "--offline",
            "--output-dir",
            str(out),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0 and json.loads(result.stdout)["reason"] == "media_hash_mismatch"
    assert not out.exists()
