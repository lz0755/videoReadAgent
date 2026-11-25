"""Step A unit and contract tests for D0 6-video / 24-question manifest validation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from eval.manifest import (
    D0_EXPECTED_TOTAL_QUESTIONS,
    D0_EXPECTED_VIDEO_COUNT,
    DatasetManifestError,
    load_dev_seed_annotations,
    load_dev_seed_manifest,
    validate_dev_seed_dataset,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SEED_MANIFEST = REPO_ROOT / "eval" / "manifests" / "dev_seed.jsonl"
DEV_SEED_ANNOTATIONS = REPO_ROOT / "eval" / "annotations" / "dev_seed" / "dev_seed_questions.json"
BENCHMARK_MANIFEST = REPO_ROOT / "experiments" / "manifests" / "benchmark_subset_manifest.json"


def _make_valid_fixture_dataset(
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for idx in range(1, 7):
        rel_path = f"clips/dev_{idx}.mp4"
        full_path = tmp_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"synthetic-dev-clip-{idx}".encode()
        full_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        video_id = f"dev_vid_{idx}"
        videos.append(
            {
                "video_id": video_id,
                "benchmark": "mme_videoocr",
                "split": "m0_dev",
                "source_group_id": f"mme_videoocr:group_{idx}",
                "original_id": f"orig_{idx}",
                "relative_path": rel_path,
                "byte_size": len(payload),
                "sha256": digest,
                "duration_ms": 30000,
                "has_audio": idx % 2 == 0,
                "m1_5_slice": idx <= 3,
                "authorization": {
                    "source_repo_url": "https://huggingface.co/datasets/DogNeverSleep/MME-VideoOCR_Dataset",
                    "license_name": "CC-BY-NC-4.0",
                    "usage_scope": "local_non_commercial_research_eval_only",
                    "commercial_use_allowed": False,
                    "redistribution_allowed": False,
                    "recorded_at_utc": "2026-11-25T00:00:00+00:00",
                },
                "annotations_path": "eval/annotations/dev_seed/dev_seed_questions.json",
            }
        )
        for q_idx in range(1, 5):
            is_unanswerable = q_idx == 4
            if is_unanswerable:
                questions.append(
                    {
                        "question_id": f"{video_id}_q{q_idx}",
                        "video_id": video_id,
                        "question_text": f"Unanswerable detail for {video_id}?",
                        "category": "unanswerable_absence",
                        "answerability": "not_answerable",
                        "reference_answer": None,
                        "expected_gaps": ["Not shown or stated anywhere in the clip."],
                        "evidence_refs": [
                            {
                                "start_ms": 0,
                                "end_ms": 30000,
                                "modality": "absent_verified",
                                "basis": "Full clip inspected; target detail never appears.",
                            }
                        ],
                        "verification_basis": "Directly checked frames across 0-30000ms.",
                    }
                )
            else:
                questions.append(
                    {
                        "question_id": f"{video_id}_q{q_idx}",
                        "video_id": video_id,
                        "question_text": f"Observable question {q_idx} for {video_id}?",
                        "category": "small_text_ocr",
                        "answerability": "answerable",
                        "reference_answer": f"Verified answer {q_idx}",
                        "expected_gaps": [],
                        "evidence_refs": [
                            {
                                "start_ms": q_idx * 1000,
                                "end_ms": q_idx * 1000 + 2000,
                                "modality": "visual",
                                "basis": f"Visible in frame at {q_idx}s.",
                            }
                        ],
                        "verification_basis": f"Verified via frame inspection at {q_idx}s.",
                    }
                )

    annotations = {
        "schema_version": "m0.3-dev-seed-annotations-v1",
        "split": "m0_dev",
        "questions": questions,
    }
    return videos, annotations


def test_repo_dev_seed_manifest_and_24_questions_valid(tmp_path: Path) -> None:
    assert DEV_SEED_MANIFEST.is_file(), "eval/manifests/dev_seed.jsonl must exist"
    assert DEV_SEED_ANNOTATIONS.is_file(), "dev_seed_questions.json must exist"
    assert BENCHMARK_MANIFEST.is_file()

    bench = json.loads(BENCHMARK_MANIFEST.read_text(encoding="utf-8"))
    holdout_groups = {
        art["source_group_id"] for art in bench["media_artifacts"] if art["split"] == "m6_holdout"
    }
    holdout_ids = {
        art["artifact_id"] for art in bench["media_artifacts"] if art["split"] == "m6_holdout"
    }

    videos = load_dev_seed_manifest(DEV_SEED_MANIFEST)
    annotations = load_dev_seed_annotations(DEV_SEED_ANNOTATIONS)

    summary = validate_dev_seed_dataset(
        videos,
        annotations,
        repo_root=REPO_ROOT,
        holdout_source_group_ids=holdout_groups,
        holdout_video_ids=holdout_ids,
    )
    assert summary.video_count == D0_EXPECTED_VIDEO_COUNT
    assert summary.question_count == D0_EXPECTED_TOTAL_QUESTIONS
    assert summary.answerable_count >= 18
    assert summary.not_answerable_count >= 3
    assert len(summary.m1_5_slice_video_ids) == 3

    if any((REPO_ROOT / v["relative_path"]).parent.exists() for v in videos):
        assert summary.verified_on_disk_files == D0_EXPECTED_VIDEO_COUNT
    else:
        assert summary.verified_on_disk_files == 0
        # Clean checkout without gitignored data/: still exercise full on-disk SHA-256 verification
        synth_videos, synth_annotations = _make_valid_fixture_dataset(tmp_path)
        synth_summary = validate_dev_seed_dataset(
            synth_videos,
            synth_annotations,
            repo_root=tmp_path,
            verify_files_on_disk=True,
            holdout_source_group_ids=holdout_groups,
            holdout_video_ids=holdout_ids,
        )
        assert synth_summary.verified_on_disk_files == D0_EXPECTED_VIDEO_COUNT


@pytest.mark.parametrize(
    "case", ["complete", "clean", "empty", "missing", "size", "hash", "partial_corrupt"]
)
def test_repo_root_checks_entire_provisioned_dataset(tmp_path: Path, case: str) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    paths = [tmp_path / v["relative_path"] for v in videos]
    assert len(paths) == D0_EXPECTED_VIDEO_COUNT
    for path, video in zip(paths, videos, strict=True):
        contents = path.read_bytes()
        assert (len(contents), hashlib.sha256(contents).hexdigest()) == (
            video["byte_size"],
            video["sha256"],
        )

    if case == "complete":
        assert validate_dev_seed_dataset(
            videos, annotations, repo_root=tmp_path
        ).verified_on_disk_files == len(paths)
        return
    if case == "clean":
        clean = tmp_path / "checkout"
        clean.mkdir()
        assert (
            validate_dev_seed_dataset(videos, annotations, repo_root=clean).verified_on_disk_files
            == 0
        )
        with pytest.raises(DatasetManifestError, match="file missing"):
            validate_dev_seed_dataset(
                videos, annotations, repo_root=clean, verify_files_on_disk=True
            )
        return
    if case in {"empty", "missing", "partial_corrupt"}:
        for path in paths if case == "empty" else paths[-1:]:
            path.unlink()
    if case == "partial_corrupt":
        for path in paths[:-1]:
            path.write_bytes(b"corrupt")
    elif case == "size":
        paths[0].write_bytes(b"short")
    elif case == "hash":
        paths[0].write_bytes(b"!" * paths[0].stat().st_size)

    error = {
        "empty": "file missing",
        "missing": "file missing",
        "size": "byte_size mismatch",
        "hash": "sha256 hash mismatch",
        "partial_corrupt": "byte_size mismatch",
    }
    with pytest.raises(DatasetManifestError, match=error[case]):
        validate_dev_seed_dataset(videos, annotations, repo_root=tmp_path)


def test_rejects_duplicate_source_group_id(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    videos[1]["source_group_id"] = videos[0]["source_group_id"]
    with pytest.raises(DatasetManifestError, match="(?i)duplicate.*source_group_id"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_duplicate_video_id(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    videos[1]["video_id"] = videos[0]["video_id"]
    with pytest.raises(DatasetManifestError, match="(?i)duplicate.*video_id"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_missing_authorization_fields(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    del videos[0]["authorization"]["license_name"]
    with pytest.raises(DatasetManifestError, match="(?i)authorization|license_name"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_missing_or_invalid_sha256(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    videos[2]["sha256"] = "not-a-sha256"
    with pytest.raises(DatasetManifestError, match="(?i)sha256"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_on_disk_sha256_mismatch(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    tampered = tmp_path / videos[0]["relative_path"]
    tampered.write_bytes(b"tampered-video-bytes")
    with pytest.raises(DatasetManifestError, match="(?i)sha256.*mismatch|hash"):
        validate_dev_seed_dataset(
            videos,
            annotations,
            repo_root=tmp_path,
            verify_files_on_disk=True,
        )


def test_rejects_wrong_video_or_question_count(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    with pytest.raises(DatasetManifestError, match="(?i)6|video"):
        validate_dev_seed_dataset(videos[:5], annotations)

    ann_short = copy.deepcopy(annotations)
    ann_short["questions"] = ann_short["questions"][:-1]
    with pytest.raises(DatasetManifestError, match="(?i)24|question|4"):
        validate_dev_seed_dataset(videos, ann_short)


def test_rejects_answerable_question_without_verifiable_evidence(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    annotations["questions"][0]["evidence_refs"] = []
    with pytest.raises(DatasetManifestError, match="(?i)evidence"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_float_or_out_of_range_evidence_timestamps(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    ann_float = copy.deepcopy(annotations)
    ann_float["questions"][0]["evidence_refs"][0]["start_ms"] = 1000.5
    with pytest.raises(DatasetManifestError, match="(?i)integer|start_ms"):
        validate_dev_seed_dataset(videos, ann_float)

    ann_oob = copy.deepcopy(annotations)
    ann_oob["questions"][0]["evidence_refs"][0]["end_ms"] = 35000
    with pytest.raises(DatasetManifestError, match="(?i)duration|range|end_ms"):
        validate_dev_seed_dataset(videos, ann_oob)


def test_rejects_not_answerable_without_absence_verification_basis(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    # Index 3 is the not_answerable question for dev_vid_1
    annotations["questions"][3]["expected_gaps"] = []
    with pytest.raises(DatasetManifestError, match="(?i)expected_gaps|not_answerable"):
        validate_dev_seed_dataset(videos, annotations)


def test_rejects_holdout_source_group_contamination(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    with pytest.raises(DatasetManifestError, match="(?i)holdout"):
        validate_dev_seed_dataset(
            videos,
            annotations,
            holdout_source_group_ids={videos[0]["source_group_id"]},
        )


def test_rejects_private_absolute_paths(tmp_path: Path) -> None:
    videos, annotations = _make_valid_fixture_dataset(tmp_path)
    videos[0]["relative_path"] = r"C:\Users\alice\private\clip.mp4"
    with pytest.raises(DatasetManifestError, match="(?i)private|absolute|path"):
        validate_dev_seed_dataset(videos, annotations)
