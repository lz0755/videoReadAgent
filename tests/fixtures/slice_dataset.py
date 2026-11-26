"""Local, synthetic media and pinned questions for offline runner contracts."""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import av
import pytest

from eval.manifest import load_dev_seed_annotations
from eval.slice_runner import load_slice_manifest, validate_slice_manifest
from tests.fixtures.media_factory import build_f01_f04_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = Path("eval/manifests/slice.jsonl")


@dataclass(frozen=True)
class SliceDataset:
    root: Path
    manifest: Path

    def subset(self, count: int, directory: Path) -> SliceDataset:
        entries = load_slice_manifest(self.manifest)[:count]
        annotations = load_dev_seed_annotations(self.root / entries[0]["annotations_path"])
        grouped = validate_slice_manifest(
            entries, annotations, repo_root=self.root, verify_files_on_disk=True
        )
        assert len(entries) == count and all(len(grouped[e["video_id"]]) == 4 for e in entries)
        path = directory / "slice.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return SliceDataset(self.root, path)


def installed_slice_cases() -> list[str]:
    """Any provisioned D0 directory activates the full strict real-media run."""
    entries = load_slice_manifest(REPO_ROOT / _MANIFEST)
    cases = ["synthetic"]
    if any((REPO_ROOT / entry["relative_path"]).parent.exists() for entry in entries):
        cases.append("authorized_d0")
    return cases


@pytest.fixture(scope="session")
def synthetic_slice(tmp_path_factory: pytest.TempPathFactory) -> SliceDataset:
    root = tmp_path_factory.mktemp("synthetic-slice")
    source_dir = root / "synthetic"
    checked = build_f01_f04_suite(source_dir, visible_clock=True)
    entries = []
    questions = []
    annotations_path = Path("eval/annotations/synthetic_slice.json")
    texts = [
        "frame 中有测试图案吗？",
        "frame 中能看到多种色块吗？",
        "frame 中背景是纯白吗？",
        "火星表面气压是多少？",
    ]
    for fid in ("F01", "F02", "F04"):
        fixture = checked["fixtures"][fid]
        assert fixture["passed"] and all(fixture["assertions"].values())
        source = source_dir / fixture["file_path"]
        video_id = f"synthetic_{fid.lower()}"
        with av.open(str(source)) as container:
            stream = container.streams.video[0]
            assert stream.duration is not None and stream.time_base is not None
            duration_ms = math.ceil(stream.duration * stream.time_base * 1000)
        ids = []
        for i, question in enumerate(texts):
            qid = f"{video_id}_q{i + 1}"
            ids.append(qid)
            questions.append(
                {
                    "question_id": qid,
                    "video_id": video_id,
                    "question_text": question,
                    "answerability": "answerable" if i < 3 else "not_answerable",
                }
            )
        entries.append(
            {
                "video_id": video_id,
                "source_group_id": f"synthetic-{fid}",
                "m1_5_slice": True,
                "split": "m0_dev",
                "benchmark": "synthetic_ffmpeg_fixture",
                "relative_path": source.relative_to(root).as_posix(),
                "sha256": fixture["sha256"],
                "byte_size": fixture["byte_size"],
                "duration_ms": duration_ms,
                "annotations_path": annotations_path.as_posix(),
                "question_ids": ids,
                "question_mix": {"answerable": 3, "not_answerable": 1},
                "authorization": {
                    "source_repo_url": "urn:video-agent:tests:synthetic-ffmpeg",
                    "license_name": "locally_generated_test_pattern",
                    "usage_scope": "offline_test",
                    "commercial_use_allowed": False,
                    "redistribution_allowed": False,
                    "recorded_at_utc": checked["generated_at_utc"],
                },
            }
        )
    manifest = root / _MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    ann = root / annotations_path
    ann.parent.mkdir()
    ann.write_text(json.dumps({"questions": questions}, ensure_ascii=False), encoding="utf-8")
    verified = validate_slice_manifest(
        load_slice_manifest(manifest),
        load_dev_seed_annotations(ann),
        repo_root=root,
        verify_files_on_disk=True,
    )
    assert [len(group) for group in verified.values()] == [4, 4, 4]
    assert len({entry["sha256"] for entry in entries}) == 3
    registry_path = Path("prompts/registry.json")
    registry = json.loads((REPO_ROOT / registry_path).read_text(encoding="utf-8"))
    files = [
        registry_path,
        *[registry_path.parent / t["relative_path"] for t in registry["templates"]],
    ]
    files.extend(
        Path("config") / name for name in ("defaults.yaml", "prices.yaml", "prices_openai.yaml")
    )
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, root / rel)
    return SliceDataset(root, manifest)
