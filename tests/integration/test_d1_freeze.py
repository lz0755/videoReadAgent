"""Exercise the evaluator-local D1 packet on labelled synthetic videos, never held-out content."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import pytest

from eval.d1 import verify_packet


def test_missing_data_cannot_publish_or_disclose_a_private_path(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.d1",
            "--repo-root",
            str(tmp_path),
            "--packet",
            str(tmp_path / "private-input-name.json"),
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode != 0 and not receipt.exists()
    assert json.loads(run.stdout)["status"] == "blocked"
    assert "private-input-name" not in run.stdout + run.stderr
    assert str(tmp_path) not in run.stdout + run.stderr and "Traceback" not in run.stderr


def _proofs(path: Path, root: Path) -> list[dict[str, Any]]:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    frames = []
    with av.open(str(path)) as video:
        for i, f in enumerate(video.decode(video=0)):
            assert f.pts is not None and f.time_base is not None
            tb = Fraction(f.time_base)
            png = f"{path.stem}-frame{i}.png"
            f.to_image().save(root / png)  # type: ignore[no-untyped-call]
            frames.append(
                {
                    "path": png,
                    "sha256": hashlib.sha256((root / png).read_bytes()).hexdigest(),
                    "source_sha256": sha,
                    "raw_pts": f.pts,
                    "actual_pts_ms": int(f.pts * tb * 1000),
                    "time_base": [tb.numerator, tb.denominator],
                    "origin_pts": 0,
                    "origin_time_base": [tb.numerator, tb.denominator],
                }
            )
    assert [f["actual_pts_ms"] for f in frames] == [0, 500, 1000, 1500, 2000, 2500]
    return frames


@pytest.fixture(scope="module")
def packet_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    root = tmp_path_factory.mktemp("d1-synthetic")
    source = root / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=48x48:rate=2:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    videos = []
    seed = []
    supplement = []
    groups = {}
    events = []
    requests = []
    for i in range(24):
        vid = f"v{i}"
        split = "m0_dev" if i < 6 else "d1_dev_extra" if i < 16 else "m6_holdout"
        path = root / f"{vid}.mp4"
        color = f"0x{i * 950729 % 0xFFFFFF:06x}"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(source),
                "-vf",
                f"drawbox=x=0:y=0:w=18:h=18:color={color}:t=fill",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
        frames = _proofs(path, root)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        videos.append(
            {
                "artifact_id": vid,
                "split": split,
                "media_kind": "video",
                "sha256": digest,
                "source_group_id": vid,
                "original_id": vid,
                "relative_path": path.name,
                "ffprobe": {"duration_ms": 3000},
            }
        )
        groups[vid] = {"series": vid, "related": vid}
        for j in range(4):
            answerable = j < 3 or i < 16
            q = {
                "question_id": f"q{i}-{j}",
                "video_id": vid,
                "question_text": f"Synthetic question {j}?",
                "category": "visual",
                "verification_basis": "synthetic frame",
                "answerability": "answerable" if answerable else "not_answerable",
                "reference_answer": "synthetic" if answerable else None,
                "expected_gaps": [] if answerable else ["synthetic gap"],
                "evidence_refs": [
                    {
                        "start_ms": 0,
                        "end_ms": 2000,
                        "basis": "fixture",
                        "modality": "visual" if answerable else "absent_verified",
                    }
                ],
            }
            (seed if i < 6 else supplement).append(q)
        if i >= 16:
            for j in range(3 if i < 20 else 2):
                eid = f"e{i}-{j}"
                proofs = [f | {"source_sha256": digest} for f in frames]
                events.append(
                    {
                        "event_id": eid,
                        "video_id": vid,
                        "label": f"event {j}",
                        "start_ms": 500,
                        "end_ms": 2000,
                        "start_frames": proofs[:2],
                        "end_frames": proofs[3:5],
                    }
                )
                requests.append(
                    {
                        "request_id": "r" + eid,
                        "event_id": eid,
                        "video_id": vid,
                        "mode": "event_localization",
                        "query_text": f"locate event {j}",
                    }
                )
    return (
        root,
        videos,
        seed,
        {
            "groups": groups,
            "questions": supplement,
            "events": events,
            "requests": requests,
            "review": {
                "coverage_note": "synthetic self-check only",
                "short_clip_decision": "synthetic short fixtures",
            },
        },
    )


@pytest.mark.parametrize(
    "fault", ["none", "wrong_split", "question_count", "request", "same_picture", "source_pts"]
)
def test_private_packet_binds_annotations_to_real_presentation_frames(
    packet_fixture: tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
    fault: str,
) -> None:
    root, videos, seed, template = packet_fixture
    packet = deepcopy(template)
    if fault == "wrong_split":
        packet["groups"]["v16"]["series"] = packet["groups"]["v1"]["series"]
    elif fault == "question_count":
        packet["questions"].pop()
    elif fault == "request":
        packet["requests"][0]["event_id"] = packet["events"][1]["event_id"]
    elif fault == "same_picture":
        ref = packet["events"][0]["start_frames"][1]
        ref.update(
            path="v16-frame4.png",
            sha256=hashlib.sha256((root / "v16-frame4.png").read_bytes()).hexdigest(),
        )
    elif fault == "source_pts":
        packet["events"][0]["start_frames"][1]["raw_pts"] += 1
    if fault == "none":
        verify_packet(videos, seed, packet, evidence_root=root, repo_root=root)
    else:
        with pytest.raises(ValueError):
            verify_packet(videos, seed, packet, evidence_root=root, repo_root=root)
