"""Contract tests for the M0 / M1.5 / M6 <=20 GB benchmark subset & M0-A1 extractor.

All tests execute 100% offline under --disable-socket --allow-unix-socket.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from experiments.dataset_curator import (
    BENCHMARK_AUTHORIZATIONS,
    DEFAULT_MANIFEST_OUT,
    MAX_DATASET_TOTAL_BYTES,
    DatasetBudgetError,
    DatasetIntegrityError,
    DatasetIsolationError,
    build_sanitized_manifest,
    enforce_dataset_size_budget,
    pixel_to_grid_region,
    validate_subset_manifest,
)
from experiments.provider_probe import (
    DEFAULT_PROVIDER_CONFIG_PATH,
    ProviderContractConfig,
    evaluate_m0_a1_extracted_samples,
    run_offline_m0_1,
)

_DUMMY_SHA_A = "a" * 64
_DUMMY_SHA_B = "b" * 64
_DUMMY_SHA_C = "c" * 64


def _make_minimal_valid_manifest() -> dict[str, Any]:
    dev_item = {
        "artifact_id": "dev_v1",
        "benchmark": "mme_videoocr",
        "split": "m0_dev",
        "media_kind": "video",
        "original_id": "941",
        "source_group_id": "mme_videoocr:941",
        "task_id": None,
        "source_video_url": "https://example.invalid/dev1",
        "relative_path": "data/datasets/mme_videoocr/m0_dev/dev_v1.mp4",
        "byte_size": 5_000_000,
        "sha256": _DUMMY_SHA_A,
        "authorization": BENCHMARK_AUTHORIZATIONS["mme_videoocr"].to_dict(),
    }
    holdout_item = {
        "artifact_id": "holdout_v1",
        "benchmark": "video_mme_v2",
        "split": "m6_holdout",
        "media_kind": "video",
        "original_id": "084",
        "source_group_id": "video_mme_v2:084",
        "task_id": None,
        "source_video_url": "https://example.invalid/holdout1",
        "relative_path": "data/datasets/video_mme_v2/m6_holdout/holdout_v1.mp4",
        "byte_size": 10_000_000,
        "sha256": _DUMMY_SHA_B,
        "authorization": BENCHMARK_AUTHORIZATIONS["video_mme_v2"].to_dict(),
    }
    m0_a1_sample = {
        "sample_id": "m0_a1_sample_1",
        "benchmark": "mme_videoocr",
        "parent_artifact_id": "dev_v1",
        "probe_purpose": "software_tutorial_high_vs_grid_roi",
        "recommended_roi_region_id": "r2c2",
        "frames": [
            {
                "frame_id": "f0",
                "relative_path": "data/datasets/m0_a1_extracted/f0.png",
                "byte_size": 120_000,
                "sha256": _DUMMY_SHA_C,
            }
        ],
    }
    return build_sanitized_manifest(
        media_artifacts=[dev_item, holdout_item],
        m1_5_slice_artifact_ids=["dev_v1"],
        m0_a1_extracted_samples=[m0_a1_sample],
        auxiliary_metadata_bytes=1024,
    )


def test_20gb_budget_cap_is_strictly_enforced() -> None:
    enforce_dataset_size_budget(MAX_DATASET_TOTAL_BYTES)
    with pytest.raises(DatasetBudgetError):
        enforce_dataset_size_budget(MAX_DATASET_TOTAL_BYTES + 1)
    with pytest.raises(DatasetBudgetError):
        enforce_dataset_size_budget(-1)

    manifest = _make_minimal_valid_manifest()
    manifest["media_artifacts"][0]["byte_size"] = MAX_DATASET_TOTAL_BYTES
    manifest["totals"]["total_bytes"] = MAX_DATASET_TOTAL_BYTES + 10_121_024
    with pytest.raises(DatasetBudgetError):
        validate_subset_manifest(manifest)


def test_dev_vs_m6_holdout_isolation_rejects_any_overlap() -> None:
    # 1. Overlapping source_group_id
    m1 = _make_minimal_valid_manifest()
    m1["media_artifacts"][1]["source_group_id"] = m1["media_artifacts"][0]["source_group_id"]
    with pytest.raises(DatasetIsolationError):
        validate_subset_manifest(m1)

    # 2. Overlapping source_video_url
    m2 = _make_minimal_valid_manifest()
    m2["media_artifacts"][1]["source_video_url"] = m2["media_artifacts"][0]["source_video_url"]
    with pytest.raises(DatasetIsolationError):
        validate_subset_manifest(m2)

    # 3. Overlapping VideoGUI task_id across dev & holdout
    m3 = _make_minimal_valid_manifest()
    m3["media_artifacts"][0]["benchmark"] = "videogui"
    m3["media_artifacts"][0]["task_id"] = "PR_10"
    m3["media_artifacts"][1]["benchmark"] = "videogui"
    m3["media_artifacts"][1]["task_id"] = "PR_10"
    with pytest.raises(DatasetIsolationError):
        validate_subset_manifest(m3)

    # 4. M1.5 slice referencing a holdout artifact
    m4 = _make_minimal_valid_manifest()
    m4["m1_5_slice_artifact_ids"] = ["holdout_v1"]
    with pytest.raises(DatasetIsolationError):
        validate_subset_manifest(m4)

    # 5. M0-A1 extracted sample derived from a holdout artifact
    m5 = _make_minimal_valid_manifest()
    m5["m0_a1_extracted_samples"][0]["parent_artifact_id"] = "holdout_v1"
    with pytest.raises(DatasetIsolationError):
        validate_subset_manifest(m5)


def test_sanitization_rejects_private_paths_and_holdout_answers() -> None:
    m_path = _make_minimal_valid_manifest()
    m_path["media_artifacts"][0]["relative_path"] = r"C:\Users\alice\secret\dev_v1.mp4"
    with pytest.raises(DatasetIntegrityError):
        validate_subset_manifest(m_path)

    m_ans = _make_minimal_valid_manifest()
    m_ans["media_artifacts"][1]["ground_truth_answer"] = "B"
    with pytest.raises(DatasetIntegrityError):
        validate_subset_manifest(m_ans)

    m_auth = _make_minimal_valid_manifest()
    m_auth["media_artifacts"][0]["authorization"] = {}
    with pytest.raises(DatasetIntegrityError):
        validate_subset_manifest(m_auth)


def test_pixel_to_grid_region_maps_corners_and_center() -> None:
    assert pixel_to_grid_region(0, 0, 1920, 1080) == "r1c1"
    assert pixel_to_grid_region(960, 540, 1920, 1080) == "r2c2"
    assert pixel_to_grid_region(1919, 1079, 1920, 1080) == "r3c3"
    assert pixel_to_grid_region(1800, 100, 1920, 1080) == "r1c3"
    assert pixel_to_grid_region(100, 1000, 1920, 1080) == "r3c1"


def test_evaluate_m0_a1_extracted_samples_verifies_sha256_roi_and_pts_order(
    tmp_path: Path,
) -> None:
    cfg = ProviderContractConfig.from_yaml(DEFAULT_PROVIDER_CONFIG_PATH)
    frame_dir = tmp_path / "data" / "datasets" / "m0_a1_extracted"
    frame_dir.mkdir(parents=True, exist_ok=True)

    gui_png = frame_dir / "gui.png"
    Image.new("RGB", (2560, 1600), color=(32, 36, 42)).save(gui_png, format="PNG")
    gui_sha = hashlib.sha256(gui_png.read_bytes()).hexdigest()

    order_frames = []
    for idx, pts_ms in enumerate((2000, 8000, 14000)):
        p = frame_dir / f"ord_{idx}.png"
        Image.new("RGB", (1280, 720), color=(40 + idx * 30, 50, 60)).save(p, format="PNG")
        order_frames.append(
            {
                "frame_id": f"ord_{idx}",
                "relative_path": f"data/datasets/m0_a1_extracted/ord_{idx}.png",
                "width": 1280,
                "height": 720,
                "byte_size": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "timestamp_kind": "ffprobe_actual_pts",
                "requested_ms": pts_ms,
                "actual_pts_ms": pts_ms + 33,
            }
        )

    manifest = {
        "schema_version": "m0-benchmark-subset-v1",
        "totals": {"total_bytes": 100_000, "within_20gb_budget": True},
        "m0_a1_extracted_samples": [
            {
                "sample_id": "vg_gui_test",
                "benchmark": "videogui",
                "parent_artifact_id": "vg_dev_1",
                "probe_purpose": "software_tutorial_high_vs_grid_roi",
                "recommended_roi_region_id": "r2c1",
                "frames": [
                    {
                        "frame_id": "gui_0",
                        "relative_path": "data/datasets/m0_a1_extracted/gui.png",
                        "width": 2560,
                        "height": 1600,
                        "byte_size": gui_png.stat().st_size,
                        "sha256": gui_sha,
                        "timestamp_kind": "static_gui_keyframe",
                    }
                ],
            },
            {
                "sample_id": "vmme_ord_test",
                "benchmark": "video_mme_v2",
                "parent_artifact_id": "vmme_dev_1",
                "probe_purpose": "vision_multi_image_order",
                "recommended_roi_region_id": "r2c2",
                "frames": order_frames,
            },
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = evaluate_m0_a1_extracted_samples(
        config=cfg,
        manifest_path=manifest_path,
        repo_root=tmp_path,
    )
    assert result["live_executed"] is False
    assert result["verified_real_frame_count"] == 4
    assert result["missing_local_frame_count"] == 0
    assert len(result["roi_comparisons"]) == 1
    roi_comp = result["roi_comparisons"][0]["comparison"]
    assert roi_comp["source_kind"] == "authorized_real_frame"
    assert roi_comp["high_guarantees_small_text_legibility"] is False
    assert roi_comp["strategy_a_original_high"]["image_dimensions"] == [[2560, 1600]]
    assert roi_comp["strategy_b_960_plus_grid_roi"]["roi_region_id"] == "r2c1"
    assert roi_comp["strategy_b_960_plus_grid_roi"]["grid_preview_is_fact_evidence"] is False

    assert len(result["multi_image_order_sequences"]) == 1
    seq = result["multi_image_order_sequences"][0]
    assert seq["pts_strictly_increasing"] is True
    assert seq["allowed_by_documented_contract"] is True
    assert seq["within_p0_index_8_image_cap"] is True

    # Tampering with a frame must raise a SHA-256 mismatch error.
    gui_png.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        evaluate_m0_a1_extracted_samples(
            config=cfg,
            manifest_path=manifest_path,
            repo_root=tmp_path,
        )


def test_curated_repo_manifest_meets_plan_and_20gb_contract(tmp_path: Path) -> None:
    assert DEFAULT_MANIFEST_OUT.exists(), f"Missing curated manifest: {DEFAULT_MANIFEST_OUT}"

    manifest = json.loads(DEFAULT_MANIFEST_OUT.read_text(encoding="utf-8"))
    validate_subset_manifest(manifest, max_total_bytes=MAX_DATASET_TOTAL_BYTES)

    totals = manifest["totals"]
    assert totals["within_20gb_budget"] is True
    assert totals["total_bytes"] <= MAX_DATASET_TOTAL_BYTES

    # All three benchmarks must be represented.
    assert set(totals["by_benchmark"].keys()) == {"mme_videoocr", "videogui", "video_mme_v2"}

    # Exact D0 / D1 video plan: 6 m0_dev + 10 d1_dev_extra = 16 dev; 8 m6_holdout = 24 total.
    videos = [a for a in manifest["media_artifacts"] if a.get("media_kind") == "video"]
    assert sum(1 for v in videos if v["split"] == "m0_dev") == 6
    assert sum(1 for v in videos if v["split"] == "d1_dev_extra") == 10
    assert sum(1 for v in videos if v["split"] == "m6_holdout") == 8
    assert len(videos) == 24
    assert len(manifest["m1_5_slice_artifact_ids"]) == 3

    # M0-A1 extracted samples must cover all 3 benchmarks.
    m0_a1_bms = {s["benchmark"] for s in manifest["m0_a1_extracted_samples"]}
    assert m0_a1_bms == {"videogui", "mme_videoocr", "video_mme_v2"}

    # Offline M0.1 probe using this manifest must NEVER fabricate runtime_verified.
    matrix, record = run_offline_m0_1(
        m0_a1_manifest_path=DEFAULT_MANIFEST_OUT,
        capabilities_out=tmp_path / "cap.json",
        report_out=tmp_path / "rep.json",
    )
    statuses = {row["capability_id"]: row["status"] for row in matrix["capabilities"]}
    assert "runtime_verified" not in statuses.values()
    assert record["budget_ledger"]["model_attempts"] == 0
