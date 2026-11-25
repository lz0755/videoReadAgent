"""Step A contract tests for M0-A3 correlated baseline_report.json and D1 schedule."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from experiments.baseline_runner import (
    BASELINE_SCHEMA_VERSION,
    DEFAULT_BASELINE_REPORT_PATH,
    BaselineReportError,
    build_d1_annotation_plan,
    run_m0_baseline,
    validate_baseline_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REPORT_FILE = REPO_ROOT / DEFAULT_BASELINE_REPORT_PATH
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)")


def _build_valid_minimal_report() -> dict[str, Any]:
    run_id = "m0-baseline-offline-20261125T104500Z"
    digest = "a" * 64
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at_utc": "2026-11-25T10:45:00+00:00",
        "execution_mode": "offline",
        "code_version": {
            "run_id": run_id,
            "git_commit": "d551c60",
            "git_branch": "main",
            "git_dirty": True,
            "workspace_code_tree_sha256": digest,
            "tracked_file_count": 10,
        },
        "machine": {
            "os": "Windows",
            "os_release": "11",
            "architecture": "AMD64",
            "cpu_logical_count": 16,
            "processor_family": "AMD64 Family 25",
            "total_ram_bytes": 50_000_000_000,
            "total_ram_gib": 46.57,
            "python_version": "3.14.6",
            "ffmpeg_version": "ffmpeg version 8.1.2",
            "ffprobe_version": "ffprobe version 8.1.2",
        },
        "dependencies": {
            "uv_lock_sha256": digest,
            "pyproject_toml_sha256": digest,
            "packages": {"openai": "3.59.0", "httpx": "0.28.1"},
            "pyav_available": False,
            "faster_whisper_available": False,
        },
        "model_and_sdk": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "configured_model_id": "deepseek-flash",
            "documented_model_version": "DeepSeek-V4.1-Flash-1017",
            "provider_config_sha256": digest,
            "capabilities_sha256": digest,
            "sdk_name": "openai",
            "sdk_version": "3.59.0",
            "runtime_verified_capability_count": 0,
        },
        "input_hashes": {
            "run_id": run_id,
            "dev_seed_manifest_sha256": digest,
            "dev_seed_annotations_sha256": digest,
            "benchmark_subset_manifest_sha256": digest,
            "f01_f04_fixture_manifest_sha256": digest,
            "m0_a1_frames_bundle_sha256": digest,
            "dev_seed_videos": [
                {
                    "video_id": f"vid_{i}",
                    "relative_path": f"data/datasets/dev/vid_{i}.mp4",
                    "byte_size": 1024,
                    "sha256": digest,
                    "duration_ms": 30000,
                }
                for i in range(6)
            ],
            "combined_inputs_sha256": digest,
        },
        "request_body_hashes": {
            "run_id": run_id,
            "offline_boundary_cases": [
                {
                    "case_id": "15_images_4097px",
                    "serialized_body_bytes": 46437,
                    "serialized_body_sha256": digest,
                    "allowed_by_documented_contract": False,
                }
            ],
            "m0_a1_roi_requests": [
                {
                    "sample_id": "m0_a1_vg_sd6_gui",
                    "strategy_a_body_bytes": 365687,
                    "strategy_a_body_sha256": digest,
                    "strategy_b_body_bytes": 299229,
                    "strategy_b_body_sha256": digest,
                }
            ],
            "m0_a1_multi_image_order_requests": [
                {
                    "sample_id": "m0_a1_vmme_001_order",
                    "frame_count": 4,
                    "serialized_body_bytes": 615275,
                    "serialized_body_sha256": digest,
                }
            ],
            "combined_request_bodies_sha256": digest,
        },
        "stage_timings_and_rtf": {
            "run_id": run_id,
            "total_run_duration_ms": 1500,
            "extrapolates_to_10min_full_pipeline": False,
            "measured_scope_note": (
                "Offline manifest, fixture self-check, ffprobe, and serialization"
            ),
            "stages": {
                "d0_manifest_validation": {"duration_ms": 50, "verified_videos": 6},
                "f01_f04_fixture_self_check": {"duration_ms": 400, "passed_fixtures": 4},
                "dev_seed_media_probe": {
                    "duration_ms": 800,
                    "total_media_duration_ms": 180000,
                    "longest_clip_duration_ms": 30000,
                    "aggregate_probe_rtf": 0.0044,
                },
                "provider_probe_offline_serialization": {"duration_ms": 250},
            },
        },
        "peak_memory": {
            "run_id": run_id,
            "measurement_method": "windows_GetProcessMemoryInfo_PeakWorkingSetSize+tracemalloc",
            "process_peak_rss_bytes": 64 * 1024 * 1024,
            "process_peak_rss_mib": 64.0,
            "python_tracemalloc_peak_bytes": 12 * 1024 * 1024,
            "python_tracemalloc_peak_mib": 12.0,
        },
        "usage_and_cost": {
            "run_id": run_id,
            "live_executed": False,
            "model_attempts": 0,
            "provider_http_attempts": 0,
            "images_sent_to_provider": 0,
            "bytes_transferred_to_provider": 0,
            "estimated_cost_usd": 0.0,
            "confirmed_cost_usd": 0.0,
            "cost_status": "confirmed",
            "live_model_usage_status": "not_tested_live",
        },
        "d1_annotation_plan": {
            "owner_handle": "lz0755",
            "owner_role": "Repository Owner / Lead Developer (single-person P0 project)",
            "effort_budget_person_days": "2-4",
            "freeze_gate_milestone": "M3_exit",
            "independent_reviewer_plan": "Target >=20% second-reviewer audit before M3 exit.",
            "remaining_deliverables": {
                "d1_dev_extra_videos_curated": 10,
                "d1_dev_extra_questions_remaining": 40,
                "m6_holdout_videos_curated": 8,
                "m6_holdout_questions_remaining": 32,
                "m6_holdout_answerable_questions": 24,
                "m6_holdout_not_answerable_questions": 8,
                "m6_holdout_events_remaining": 20,
            },
            "schedule": [
                {"phase": "D1-1", "milestone_window": "M1", "target": "Annotate 20 extra dev Qs"},
                {
                    "phase": "D1-2",
                    "milestone_window": "M1.5-M2",
                    "target": "Annotate 20 extra dev Qs",
                },
                {
                    "phase": "D1-3",
                    "milestone_window": "M2-M3",
                    "target": "32 holdout Qs + 20 events",
                },
                {"phase": "D1-4", "milestone_window": "M3_exit", "target": "Audit >=20% & freeze"},
            ],
        },
        "decision_and_gates": {
            "adr_path": "docs/decisions/ADR-001.md",
            "verdict": "pause_pending_live_verification",
            "m0_a1_status": "offline_verified_pending_live_credentials_and_egress_consent",
            "m0_a2_status": "passed_offline",
            "m0_a3_status": "baseline_recorded_pending_live_usage_and_independent_review",
        },
    }


def test_repo_baseline_report_exists_and_passes_full_contract() -> None:
    assert BASELINE_REPORT_FILE.is_file(), "experiments/reports/baseline_report.json must exist"
    raw_text = BASELINE_REPORT_FILE.read_text(encoding="utf-8")
    assert not PRIVATE_PATH_RE.search(raw_text)

    report = json.loads(raw_text)
    validate_baseline_report(report)

    assert report["schema_version"] == BASELINE_SCHEMA_VERSION
    assert report["execution_mode"] == "offline"
    assert report["usage_and_cost"]["live_executed"] is False
    assert report["usage_and_cost"]["model_attempts"] == 0
    assert report["usage_and_cost"]["cost_status"] == "confirmed"
    assert report["peak_memory"]["process_peak_rss_bytes"] > 0
    assert report["peak_memory"]["python_tracemalloc_peak_bytes"] > 0
    assert len(report["input_hashes"]["dev_seed_videos"]) == 6
    assert len(report["request_body_hashes"]["offline_boundary_cases"]) == 8
    assert len(report["request_body_hashes"]["m0_a1_roi_requests"]) >= 8
    assert len(report["request_body_hashes"]["m0_a1_multi_image_order_requests"]) >= 3


def _materialize_synthetic_baseline_repo(synth_root: Path) -> Path:
    """Create a self-contained mini-repo with real FFmpeg synthetic MP4s and PNG frames."""
    import hashlib
    import shutil
    import subprocess

    from PIL import Image

    synth_root.mkdir(parents=True, exist_ok=True)
    for rel in (
        "AGENTS.md",
        "pyproject.toml",
        "uv.lock",
        "experiments/config/provider_deepseek.yaml",
        "experiments/capabilities.json",
        "tests/fixtures/manifests/f01_f04_manifest.json",
    ):
        src = REPO_ROOT / rel
        dst = synth_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Generate one real 1-second synthetic H.264 MP4 via FFmpeg and replicate for 6 D0 entries
    media_dir = synth_root / "data" / "datasets" / "synth_dev"
    media_dir.mkdir(parents=True, exist_ok=True)
    template_mp4 = media_dir / "_template.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(template_mp4),
        ],
        check=True,
        capture_output=True,
    )
    mp4_bytes = template_mp4.read_bytes()
    template_mp4.unlink()
    mp4_sha = hashlib.sha256(mp4_bytes).hexdigest()

    dev_lines: list[str] = []
    questions: list[dict[str, Any]] = []
    for idx in range(1, 7):
        vid_id = f"synth_dev_{idx}"
        rel_video = f"data/datasets/synth_dev/{vid_id}.mp4"
        (synth_root / rel_video).write_bytes(mp4_bytes)
        dev_lines.append(
            json.dumps(
                {
                    "video_id": vid_id,
                    "benchmark": "mme_videoocr",
                    "split": "m0_dev",
                    "source_group_id": f"synth:group_{idx}",
                    "original_id": f"orig_{idx}",
                    "relative_path": rel_video,
                    "byte_size": len(mp4_bytes),
                    "sha256": mp4_sha,
                    "duration_ms": 1000,
                    "video_codec": "h264",
                    "has_audio": False,
                    "m1_5_slice": idx <= 3,
                    "authorization": {
                        "source_repo_url": "https://example.invalid/synth",
                        "license_name": "MIT",
                        "usage_scope": "offline_synthetic_ci_test",
                        "commercial_use_allowed": False,
                        "redistribution_allowed": False,
                        "recorded_at_utc": "2026-11-25T00:00:00+00:00",
                    },
                    "annotations_path": "eval/annotations/dev_seed/dev_seed_questions.json",
                }
            )
        )
        for q_idx in range(1, 5):
            if q_idx == 4:
                questions.append(
                    {
                        "question_id": f"{vid_id}_q{q_idx}",
                        "video_id": vid_id,
                        "question_text": f"Absent detail in {vid_id}?",
                        "category": "unanswerable_absence",
                        "answerability": "not_answerable",
                        "reference_answer": None,
                        "expected_gaps": ["Not shown anywhere in synthetic clip."],
                        "evidence_refs": [
                            {
                                "start_ms": 0,
                                "end_ms": 1000,
                                "modality": "absent_verified",
                                "basis": "Full clip checked.",
                            }
                        ],
                        "verification_basis": "Synthetic absence check.",
                    }
                )
            else:
                questions.append(
                    {
                        "question_id": f"{vid_id}_q{q_idx}",
                        "video_id": vid_id,
                        "question_text": f"Visible detail {q_idx} in {vid_id}?",
                        "category": "small_text_ocr",
                        "answerability": "answerable",
                        "reference_answer": f"detail_{q_idx}",
                        "expected_gaps": [],
                        "evidence_refs": [
                            {
                                "start_ms": 0,
                                "end_ms": 500,
                                "modality": "visual",
                                "basis": "Visible in synthetic frame.",
                            }
                        ],
                        "verification_basis": "Synthetic frame check.",
                    }
                )

    dev_manifest = synth_root / "eval/manifests/dev_seed.jsonl"
    dev_manifest.parent.mkdir(parents=True, exist_ok=True)
    dev_manifest.write_text("\n".join(dev_lines) + "\n", encoding="utf-8")

    dev_ann = synth_root / "eval/annotations/dev_seed/dev_seed_questions.json"
    dev_ann.parent.mkdir(parents=True, exist_ok=True)
    dev_ann.write_text(
        json.dumps(
            {
                "schema_version": "m0.3-dev-seed-annotations-v1",
                "split": "m0_dev",
                "questions": questions,
            }
        ),
        encoding="utf-8",
    )

    # Real synthetic PNG frames for M0-A1 ROI & multi-image order stages
    frame_dir = synth_root / "data/datasets/m0_a1_extracted"
    frame_dir.mkdir(parents=True, exist_ok=True)
    gui_png = frame_dir / "gui_0.png"
    Image.new("RGB", (1920, 1080), color=(32, 48, 64)).save(gui_png, format="PNG")
    gui_sha = hashlib.sha256(gui_png.read_bytes()).hexdigest()

    order_frames: list[dict[str, Any]] = []
    for idx, pts_ms in enumerate((100, 400, 700)):
        fp = frame_dir / f"ord_{idx}.png"
        Image.new("RGB", (640, 360), color=(40 + idx * 30, 50, 60)).save(fp, format="PNG")
        order_frames.append(
            {
                "frame_id": f"ord_{idx}",
                "relative_path": f"data/datasets/m0_a1_extracted/ord_{idx}.png",
                "width": 640,
                "height": 360,
                "byte_size": fp.stat().st_size,
                "sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
                "timestamp_kind": "ffprobe_actual_pts",
                "requested_ms": pts_ms,
                "actual_pts_ms": pts_ms,
            }
        )

    bench_manifest = synth_root / "experiments/manifests/benchmark_subset_manifest.json"
    bench_manifest.parent.mkdir(parents=True, exist_ok=True)
    bench_manifest.write_text(
        json.dumps(
            {
                "schema_version": "m0-benchmark-subset-v1",
                "media_artifacts": [
                    {
                        "artifact_id": "holdout_synth_1",
                        "split": "m6_holdout",
                        "source_group_id": "synth:holdout_1",
                    }
                ],
                "m0_a1_extracted_samples": [
                    {
                        "sample_id": "synth_roi_1",
                        "benchmark": "videogui",
                        "parent_artifact_id": "synth_dev_1",
                        "probe_purpose": "software_tutorial_high_vs_grid_roi",
                        "recommended_roi_region_id": "r2c2",
                        "frames": [
                            {
                                "frame_id": "gui_0",
                                "relative_path": "data/datasets/m0_a1_extracted/gui_0.png",
                                "width": 1920,
                                "height": 1080,
                                "byte_size": gui_png.stat().st_size,
                                "sha256": gui_sha,
                                "timestamp_kind": "static_gui_keyframe",
                            }
                        ],
                    },
                    {
                        "sample_id": "synth_order_1",
                        "benchmark": "video_mme_v2",
                        "parent_artifact_id": "synth_dev_2",
                        "probe_purpose": "vision_multi_image_order",
                        "recommended_roi_region_id": "r2c2",
                        "frames": order_frames,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return synth_root


def test_run_m0_baseline_produces_correlated_single_run_report(tmp_path: Path) -> None:
    out_file = tmp_path / "baseline_report.json"
    fixtures_dir = tmp_path / "f01_f04"
    synth_repo = _materialize_synthetic_baseline_repo(tmp_path / "synth_repo")

    report = run_m0_baseline(
        repo_root=synth_repo,
        output_path=out_file,
        fixtures_dir=fixtures_dir,
    )
    assert out_file.is_file()
    raw_text = out_file.read_text(encoding="utf-8")
    assert not PRIVATE_PATH_RE.search(raw_text)

    validate_baseline_report(report)

    run_id = report["run_id"]
    for section in (
        "code_version",
        "input_hashes",
        "request_body_hashes",
        "stage_timings_and_rtf",
        "peak_memory",
        "usage_and_cost",
    ):
        assert report[section]["run_id"] == run_id

    assert SHA256_RE.fullmatch(report["code_version"]["workspace_code_tree_sha256"])
    assert SHA256_RE.fullmatch(report["input_hashes"]["combined_inputs_sha256"])
    assert SHA256_RE.fullmatch(report["request_body_hashes"]["combined_request_bodies_sha256"])
    assert report["stage_timings_and_rtf"]["total_run_duration_ms"] >= 0
    assert report["stage_timings_and_rtf"]["extrapolates_to_10min_full_pipeline"] is False
    assert report["peak_memory"]["process_peak_rss_bytes"] > 0
    assert report["peak_memory"]["python_tracemalloc_peak_bytes"] > 0


def test_validate_rejects_mismatched_run_id_correlation() -> None:
    report = _build_valid_minimal_report()
    report["usage_and_cost"]["run_id"] = "different-run-id"
    with pytest.raises(BaselineReportError, match="(?i)run_id"):
        validate_baseline_report(report)


def test_validate_rejects_private_paths() -> None:
    report = _build_valid_minimal_report()
    report["stage_timings_and_rtf"]["measured_scope_note"] = r"Ran in C:\Users\alice\repo"
    with pytest.raises(BaselineReportError, match="(?i)private"):
        validate_baseline_report(report)


def test_validate_rejects_missing_or_invalid_request_body_hash() -> None:
    report = _build_valid_minimal_report()
    report["request_body_hashes"]["offline_boundary_cases"][0]["serialized_body_sha256"] = "bad"
    with pytest.raises(BaselineReportError, match="(?i)sha256"):
        validate_baseline_report(report)


def test_validate_rejects_zero_peak_memory() -> None:
    report = _build_valid_minimal_report()
    report["peak_memory"]["process_peak_rss_bytes"] = 0
    with pytest.raises(BaselineReportError, match="(?i)peak_rss|memory"):
        validate_baseline_report(report)


def test_validate_rejects_10min_extrapolation_or_inconsistent_live_usage() -> None:
    extrap = _build_valid_minimal_report()
    extrap["stage_timings_and_rtf"]["extrapolates_to_10min_full_pipeline"] = True
    with pytest.raises(BaselineReportError, match="(?i)extrapolat"):
        validate_baseline_report(extrap)

    bad_live = _build_valid_minimal_report()
    bad_live["usage_and_cost"]["live_executed"] = True
    bad_live["usage_and_cost"]["model_attempts"] = 0
    with pytest.raises(BaselineReportError, match="(?i)live|attempt"):
        validate_baseline_report(bad_live)


def test_validate_rejects_incomplete_d1_annotation_plan() -> None:
    plan_check = build_d1_annotation_plan()
    assert plan_check["freeze_gate_milestone"] == "M3_exit"
    assert plan_check["remaining_deliverables"]["d1_dev_extra_questions_remaining"] == 40
    assert plan_check["remaining_deliverables"]["m6_holdout_questions_remaining"] == 32
    assert plan_check["remaining_deliverables"]["m6_holdout_events_remaining"] == 20

    report = _build_valid_minimal_report()
    no_owner = copy.deepcopy(report)
    no_owner["d1_annotation_plan"]["owner_handle"] = ""
    with pytest.raises(BaselineReportError, match="(?i)owner"):
        validate_baseline_report(no_owner)

    bad_events = copy.deepcopy(report)
    bad_events["d1_annotation_plan"]["remaining_deliverables"]["m6_holdout_events_remaining"] = 0
    with pytest.raises(BaselineReportError, match="(?i)event|20"):
        validate_baseline_report(bad_events)
