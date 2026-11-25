"""M0-A3 correlated baseline runner and baseline_report.json contract validator.

Correlates code version, sanitized machine metadata, dependency lock hashes,
model/SDK config, input hashes, serialized request body hashes, per-stage
timings & RTF, real peak memory (OS working set + Python tracemalloc), usage &
cost status, and the D1 annotation owner/schedule to a SINGLE run_id.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.manifest import (
    load_dev_seed_annotations,
    load_dev_seed_manifest,
    validate_dev_seed_dataset,
)
from experiments.probe_gate import sanitize_record
from experiments.provider_probe import (
    DEFAULT_CAPABILITIES_OUT,
    DEFAULT_PROVIDER_CONFIG_PATH,
    ProviderContractConfig,
    evaluate_m0_a1_extracted_samples,
    run_offline_boundary_suite,
)
from tests.fixtures.media_factory import (
    build_f01_f04_suite,
    get_ffmpeg_version,
)

BASELINE_SCHEMA_VERSION: str = "m0-baseline-report-v1"
DEFAULT_BASELINE_REPORT_PATH: Path = Path("experiments/reports/baseline_report.json")
DEFAULT_FIXTURES_DIR: Path = Path("data/fixtures/f01_f04")
ALLOWED_COST_STATUSES: frozenset[str] = frozenset({"estimated", "confirmed", "unknown"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\+Users\\+|/Users/|/home/)")
CORRELATED_SECTIONS: tuple[str, ...] = (
    "code_version",
    "input_hashes",
    "request_body_hashes",
    "stage_timings_and_rtf",
    "peak_memory",
    "usage_and_cost",
)
KEY_PACKAGES: tuple[str, ...] = (
    "httpx",
    "openai",
    "pillow",
    "pyyaml",
    "pytest",
    "pytest-socket",
    "ruff",
    "mypy",
    "import-linter",
)
CODE_TREE_ROOTS: tuple[str, ...] = (
    "AGENTS.md",
    "pyproject.toml",
    "uv.lock",
    "src/video_agent",
    "experiments",
    "eval",
    "tests",
)
CODE_TREE_EXCLUDED_RELATIVE: frozenset[str] = frozenset(
    {
        "experiments/reports/baseline_report.json",
    }
)


class BaselineReportError(ValueError):
    """Raised when a baseline report violates M0-A3 correlation or integrity contracts."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_canonical_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(raw)


def _get_ffprobe_version() -> str:
    if shutil.which("ffprobe") is None:
        raise BaselineReportError("ffprobe binary is required on PATH")
    proc = subprocess.run(
        ["ffprobe", "-version"],
        check=True,
        capture_output=True,
        text=True,
    )
    first = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not first:
        raise BaselineReportError("Unable to parse ffprobe -version")
    return first


def _get_total_ram_bytes() -> int:
    if sys.platform == "win32":
        import ctypes.wintypes as wt

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wt.DWORD),
                ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(mem)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return int(mem.ullTotalPhys)
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return int(pages * page_size)
        except ValueError, OSError:
            pass
    return 0


def measure_process_peak_rss_bytes() -> tuple[int, str]:
    """Return OS-level peak working set / RSS bytes and the measurement method name."""
    if sys.platform == "win32":
        import ctypes.wintypes as wt

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wt.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok or counters.PeakWorkingSetSize <= 0:
            raise BaselineReportError("Windows GetProcessMemoryInfo failed to read peak RSS")
        return int(counters.PeakWorkingSetSize), "windows_GetProcessMemoryInfo_PeakWorkingSetSize"

    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    # Linux returns KiB, macOS returns bytes.
    rss_bytes = int(ru.ru_maxrss) if sys.platform == "darwin" else int(ru.ru_maxrss) * 1024
    if rss_bytes <= 0:
        raise BaselineReportError("POSIX getrusage returned non-positive ru_maxrss")
    return rss_bytes, "posix_getrusage_ru_maxrss"


def collect_sanitized_machine_info() -> dict[str, Any]:
    """Collect machine hardware/OS/runtime metadata without private paths or usernames."""
    total_ram_bytes = _get_total_ram_bytes()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu_logical_count": os.cpu_count() or 0,
        "processor_family": platform.processor(),
        "total_ram_bytes": total_ram_bytes,
        "total_ram_gib": round(total_ram_bytes / (1024**3), 2) if total_ram_bytes > 0 else 0.0,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "ffmpeg_version": get_ffmpeg_version(),
        "ffprobe_version": _get_ffprobe_version(),
    }


def _run_git(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def collect_code_version_info(repo_root: Path) -> dict[str, Any]:
    """Collect git commit/branch/dirty state and deterministic code tree SHA-256."""
    root = repo_root.resolve()
    commit = _run_git(root, ["rev-parse", "HEAD"])
    short_commit = _run_git(root, ["rev-parse", "--short", "HEAD"])
    branch = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    status_porcelain = _run_git(root, ["status", "--porcelain"])
    git_dirty = bool(status_porcelain)

    file_entries: list[tuple[str, str]] = []
    for rel_item in CODE_TREE_ROOTS:
        target = root / rel_item
        if target.is_file():
            rel_posix = target.relative_to(root).as_posix()
            if rel_posix not in CODE_TREE_EXCLUDED_RELATIVE:
                file_entries.append((rel_posix, _sha256_file(target)))
        elif target.is_dir():
            for child in sorted(target.rglob("*")):
                if not child.is_file():
                    continue
                if "__pycache__" in child.parts or child.suffix == ".pyc":
                    continue
                rel_posix = child.relative_to(root).as_posix()
                if rel_posix in CODE_TREE_EXCLUDED_RELATIVE:
                    continue
                file_entries.append((rel_posix, _sha256_file(child)))

    file_entries.sort(key=lambda item: item[0])
    tree_hash = _sha256_canonical_json(file_entries)

    return {
        "git_commit": commit,
        "git_short_commit": short_commit,
        "git_branch": branch,
        "git_dirty": git_dirty,
        "workspace_code_tree_sha256": tree_hash,
        "tracked_file_count": len(file_entries),
    }


def _collect_dependencies_info(repo_root: Path) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for pkg in KEY_PACKAGES:
        try:
            packages[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            packages[pkg] = "not_installed"
    return {
        "uv_lock_sha256": _sha256_file(repo_root / "uv.lock"),
        "pyproject_toml_sha256": _sha256_file(repo_root / "pyproject.toml"),
        "packages": packages,
        "pyav_available": importlib.util.find_spec("av") is not None,
        "faster_whisper_available": importlib.util.find_spec("faster_whisper") is not None,
    }


def build_d1_annotation_plan() -> dict[str, Any]:
    """Return structured D1 annotation owner, budget, deliverables, and M1-M3 freeze schedule."""
    return {
        "owner_handle": "lz0755",
        "owner_role": (
            "Repository Owner / Lead Developer "
            "(single-person P0 project; explicit single-annotator disclosure per Plan §7.1)"
        ),
        "effort_budget_person_days": "2-4",
        "budget_separation_note": (
            "D1 annotation effort (2-4 person-days) is tracked separately from the 25 net "
            "engineering person-days and must not be compressed by skipping tests."
        ),
        "freeze_gate_milestone": "M3_exit",
        "freeze_rule": (
            "All 24 videos (16 dev / 64 Qs + 8 holdout / 32 Qs) and 20 separate holdout "
            "event_localization annotations must be frozen with SHA-256 hashes before M4 formal "
            "QA tuning begins. M6 holdout answers remain strictly isolated from M0/M1.5/M4."
        ),
        "independent_reviewer_plan": (
            "Target >=20% second-reviewer verification audit on formal answers prior to M3 exit "
            "freeze. If single-person staffing persists at M3 exit, run a documented delayed "
            "blind self-audit (>=7-day cooling window) and disclose single-reviewer limitation "
            "in the M6 evaluation report per Plan §7.1."
        ),
        "raw_media_readiness": (
            "All 24 benchmark videos (6 m0_dev + 10 d1_dev_extra + 8 m6_holdout; "
            "0.4178 GB <= 20 GB cap) are already curated, ffprobe-checked, SHA-256 hashed, "
            "and source-group isolated under gitignored data/datasets/."
        ),
        "remaining_deliverables": {
            "d0_dev_videos_completed": 6,
            "d0_dev_questions_completed": 24,
            "d1_dev_extra_videos_curated": 10,
            "d1_dev_extra_questions_remaining": 40,
            "m6_holdout_videos_curated": 8,
            "m6_holdout_questions_remaining": 32,
            "m6_holdout_answerable_questions": 24,
            "m6_holdout_not_answerable_questions": 8,
            "m6_holdout_events_remaining": 20,
            "total_frozen_videos_at_m3_exit": 24,
            "total_frozen_standard_questions_at_m3_exit": 96,
            "total_frozen_event_requests_at_m3_exit": 20,
        },
        "schedule": [
            {
                "phase": "D1-1",
                "milestone_window": "During M1 (Engineering Skeleton & Contracts)",
                "owner": "lz0755",
                "effort_estimate": "0.5-1.0 person-day",
                "target": (
                    "Annotate first 5 d1_dev_extra videos (vg_ae11_start, vg_cc11_start, "
                    "ocr_905, ocr_903, ocr_1243) -> +20 verifiable dev questions."
                ),
            },
            {
                "phase": "D1-2",
                "milestone_window": "During M1.5-M2 (Vertical Slice & Media Hardening)",
                "owner": "lz0755",
                "effort_estimate": "0.5-1.0 person-day",
                "target": (
                    "Annotate remaining 5 d1_dev_extra videos (ocr_935, vmme_011, vmme_082, "
                    "vmme_104, vmme_113) -> +20 dev questions (64 dev Qs complete)."
                ),
            },
            {
                "phase": "D1-3",
                "milestone_window": "During M2-M3 (Timeline & Indexing)",
                "owner": "lz0755 (isolated holdout vault only)",
                "effort_estimate": "0.5-1.0 person-day",
                "target": (
                    "Curate & verify 32 isolated m6_holdout Qs (24 answerable + 8 not_answerable) "
                    "plus 20 visible-boundary holdout events for M4.5/M6 event_localization."
                ),
            },
            {
                "phase": "D1-4",
                "milestone_window": "M3_exit (Hard Gate Before M4 Tuning)",
                "owner": "lz0755 + independent reviewer (>=20% audit or disclosed delayed audit)",
                "effort_estimate": "0.5-1.0 person-day",
                "target": (
                    "Audit >=20% answers & event boundaries, verify zero dev/holdout overlap, "
                    "freeze manifests, annotations, SHA-256 hashes, and metric scripts."
                ),
            },
        ],
    }


def _probe_video_packets_ffprobe(video_path: Path) -> dict[str, Any]:
    """Run real packet-level ffprobe inspection on a video and return counts & elapsed ms."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_format",
            "-show_streams",
            "-show_packets",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "json",
            str(video_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
    if proc.returncode != 0:
        raise BaselineReportError(f"ffprobe failed for {video_path.name}: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    packets = payload.get("packets", [])
    streams = payload.get("streams", [])
    v_codec = str(streams[0].get("codec_name", "unknown")) if streams else "unknown"
    return {
        "video_packet_count": len(packets),
        "video_codec": v_codec,
        "probe_duration_ms": elapsed_ms,
    }


def _check_sha256(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise BaselineReportError(f"{field_name} must be a 64-char lowercase hex sha256")


def validate_baseline_report(report: dict[str, Any]) -> None:
    """Validate M0-A3 single-run correlation, hashes, peak memory, cost status, and D1 plan."""
    if not isinstance(report, dict):
        raise BaselineReportError("Baseline report root must be a JSON object")

    serialized = json.dumps(report, ensure_ascii=False)
    if PRIVATE_PATH_RE.search(serialized):
        raise BaselineReportError("Private absolute path detected in baseline report")

    if report.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise BaselineReportError(
            f"Unsupported schema_version: {report.get('schema_version')} "
            f"(expected {BASELINE_SCHEMA_VERSION})"
        )

    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise BaselineReportError("Baseline report requires a non-empty run_id")

    for section_name in CORRELATED_SECTIONS:
        section = report.get(section_name)
        if not isinstance(section, dict):
            raise BaselineReportError(f"Missing required correlated section: {section_name}")
        if section.get("run_id") != run_id:
            raise BaselineReportError(
                f"Correlated section '{section_name}' run_id mismatch: "
                f"expected {run_id!r}, got {section.get('run_id')!r}"
            )

    code_ver = report["code_version"]
    _check_sha256(
        code_ver.get("workspace_code_tree_sha256"),
        "code_version.workspace_code_tree_sha256",
    )
    if not isinstance(code_ver.get("git_commit"), str) or not code_ver["git_commit"].strip():
        raise BaselineReportError("code_version.git_commit must be non-empty")

    machine = report.get("machine")
    if not isinstance(machine, dict):
        raise BaselineReportError("Missing required section: machine")
    for req_key in ("os", "architecture", "python_version", "ffmpeg_version", "ffprobe_version"):
        if not isinstance(machine.get(req_key), str) or not str(machine[req_key]).strip():
            raise BaselineReportError(f"machine.{req_key} must be non-empty string")
    if not isinstance(machine.get("cpu_logical_count"), int) or machine["cpu_logical_count"] <= 0:
        raise BaselineReportError("machine.cpu_logical_count must be positive int")

    deps = report.get("dependencies")
    if not isinstance(deps, dict):
        raise BaselineReportError("Missing required section: dependencies")
    _check_sha256(deps.get("uv_lock_sha256"), "dependencies.uv_lock_sha256")
    _check_sha256(deps.get("pyproject_toml_sha256"), "dependencies.pyproject_toml_sha256")

    model_sdk = report.get("model_and_sdk")
    if not isinstance(model_sdk, dict):
        raise BaselineReportError("Missing required section: model_and_sdk")
    _check_sha256(
        model_sdk.get("provider_config_sha256"),
        "model_and_sdk.provider_config_sha256",
    )
    _check_sha256(
        model_sdk.get("capabilities_sha256"),
        "model_and_sdk.capabilities_sha256",
    )
    if (
        not isinstance(model_sdk.get("configured_model_id"), str)
        or not model_sdk["configured_model_id"].strip()
    ):
        raise BaselineReportError("model_and_sdk.configured_model_id must be non-empty")

    inputs = report["input_hashes"]
    for key in (
        "dev_seed_manifest_sha256",
        "dev_seed_annotations_sha256",
        "benchmark_subset_manifest_sha256",
        "f01_f04_fixture_manifest_sha256",
        "m0_a1_frames_bundle_sha256",
        "combined_inputs_sha256",
    ):
        _check_sha256(inputs.get(key), f"input_hashes.{key}")
    dev_videos = inputs.get("dev_seed_videos")
    if not isinstance(dev_videos, list) or len(dev_videos) != 6:
        raise BaselineReportError("input_hashes.dev_seed_videos must contain all 6 D0 dev videos")
    for idx, vid in enumerate(dev_videos):
        _check_sha256(vid.get("sha256"), f"input_hashes.dev_seed_videos[{idx}].sha256")

    req_hashes = report["request_body_hashes"]
    _check_sha256(
        req_hashes.get("combined_request_bodies_sha256"),
        "request_body_hashes.combined_request_bodies_sha256",
    )
    boundary_cases = req_hashes.get("offline_boundary_cases")
    if not isinstance(boundary_cases, list) or len(boundary_cases) == 0:
        raise BaselineReportError("request_body_hashes.offline_boundary_cases must be non-empty")
    for idx, case in enumerate(boundary_cases):
        _check_sha256(
            case.get("serialized_body_sha256"),
            f"request_body_hashes.offline_boundary_cases[{idx}].serialized_body_sha256",
        )
    roi_reqs = req_hashes.get("m0_a1_roi_requests")
    if not isinstance(roi_reqs, list) or len(roi_reqs) == 0:
        raise BaselineReportError("request_body_hashes.m0_a1_roi_requests must be non-empty")
    for idx, req in enumerate(roi_reqs):
        _check_sha256(
            req.get("strategy_a_body_sha256"),
            f"request_body_hashes.m0_a1_roi_requests[{idx}].strategy_a_body_sha256",
        )
        _check_sha256(
            req.get("strategy_b_body_sha256"),
            f"request_body_hashes.m0_a1_roi_requests[{idx}].strategy_b_body_sha256",
        )
    order_reqs = req_hashes.get("m0_a1_multi_image_order_requests")
    if not isinstance(order_reqs, list) or len(order_reqs) == 0:
        raise BaselineReportError(
            "request_body_hashes.m0_a1_multi_image_order_requests must be non-empty"
        )
    for idx, req in enumerate(order_reqs):
        _check_sha256(
            req.get("serialized_body_sha256"),
            f"request_body_hashes.m0_a1_multi_image_order_requests[{idx}].serialized_body_sha256",
        )

    timings = report["stage_timings_and_rtf"]
    if timings.get("extrapolates_to_10min_full_pipeline") is not False:
        raise BaselineReportError(
            "Offline M0 baseline must not claim 10-minute full-pipeline extrapolation"
        )
    total_ms = timings.get("total_run_duration_ms")
    if not isinstance(total_ms, int) or isinstance(total_ms, bool) or total_ms < 0:
        raise BaselineReportError("stage_timings_and_rtf.total_run_duration_ms must be int >= 0")
    stages = timings.get("stages")
    if not isinstance(stages, dict) or len(stages) < 4:
        raise BaselineReportError("stage_timings_and_rtf.stages must include all 4 baseline stages")

    mem = report["peak_memory"]
    peak_rss = mem.get("process_peak_rss_bytes")
    if not isinstance(peak_rss, int) or isinstance(peak_rss, bool) or peak_rss <= 0:
        raise BaselineReportError("peak_memory.process_peak_rss_bytes must be positive int")
    peak_py = mem.get("python_tracemalloc_peak_bytes")
    if not isinstance(peak_py, int) or isinstance(peak_py, bool) or peak_py <= 0:
        raise BaselineReportError("peak_memory.python_tracemalloc_peak_bytes must be positive int")

    usage = report["usage_and_cost"]
    cost_status = usage.get("cost_status")
    if cost_status not in ALLOWED_COST_STATUSES:
        raise BaselineReportError(f"Invalid usage_and_cost.cost_status: {cost_status}")
    live_executed = usage.get("live_executed")
    model_attempts = usage.get("model_attempts")
    if not isinstance(live_executed, bool):
        raise BaselineReportError("usage_and_cost.live_executed must be bool")
    if (
        not isinstance(model_attempts, int)
        or isinstance(model_attempts, bool)
        or model_attempts < 0
    ):
        raise BaselineReportError("usage_and_cost.model_attempts must be int >= 0")
    if live_executed and model_attempts == 0:
        raise BaselineReportError("usage_and_cost cannot mark live_executed=True with 0 attempts")
    if not live_executed and model_attempts != 0:
        raise BaselineReportError("Offline baseline cannot report non-zero live model_attempts")

    d1 = report.get("d1_annotation_plan")
    if not isinstance(d1, dict):
        raise BaselineReportError("Missing required section: d1_annotation_plan")
    if not isinstance(d1.get("owner_handle"), str) or not d1["owner_handle"].strip():
        raise BaselineReportError("d1_annotation_plan.owner_handle must be non-empty")
    if not isinstance(d1.get("owner_role"), str) or not d1["owner_role"].strip():
        raise BaselineReportError("d1_annotation_plan.owner_role must be non-empty")
    if d1.get("freeze_gate_milestone") != "M3_exit":
        raise BaselineReportError("d1_annotation_plan.freeze_gate_milestone must be 'M3_exit'")
    rem = d1.get("remaining_deliverables")
    if not isinstance(rem, dict):
        raise BaselineReportError("d1_annotation_plan.remaining_deliverables must be a dict")
    if rem.get("d1_dev_extra_questions_remaining") != 40:
        raise BaselineReportError("d1_dev_extra_questions_remaining must equal 40")
    if rem.get("m6_holdout_questions_remaining") != 32:
        raise BaselineReportError("m6_holdout_questions_remaining must equal 32")
    if rem.get("m6_holdout_events_remaining") != 20:
        raise BaselineReportError("m6_holdout_events_remaining must equal 20")
    schedule = d1.get("schedule")
    if not isinstance(schedule, list) or len(schedule) < 3:
        raise BaselineReportError("d1_annotation_plan.schedule must define phased milestones")

    decision = report.get("decision_and_gates")
    if not isinstance(decision, dict) or not decision.get("adr_path"):
        raise BaselineReportError("decision_and_gates.adr_path must be specified")


def run_m0_baseline(
    *,
    repo_root: Path | None = None,
    output_path: Path | None = DEFAULT_BASELINE_REPORT_PATH,
    fixtures_dir: Path | None = None,
) -> dict[str, Any]:
    """Run all M0 offline baseline stages in one correlated run and write baseline_report.json."""
    root = (repo_root or Path.cwd()).resolve()
    run_id = f"m0-baseline-offline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    run_start = time.perf_counter()

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()

    try:
        # Stage 1: D0 6-video / 24-question manifest + disk SHA-256 + holdout isolation check
        t1 = time.perf_counter()
        dev_manifest_path = root / "eval/manifests/dev_seed.jsonl"
        dev_annotations_path = root / "eval/annotations/dev_seed/dev_seed_questions.json"
        benchmark_manifest_path = root / "experiments/manifests/benchmark_subset_manifest.json"
        fixture_manifest_path = root / "tests/fixtures/manifests/f01_f04_manifest.json"

        bench_data = json.loads(benchmark_manifest_path.read_text(encoding="utf-8"))
        holdout_groups = {
            art["source_group_id"]
            for art in bench_data["media_artifacts"]
            if art["split"] == "m6_holdout"
        }
        holdout_ids = {
            art["artifact_id"]
            for art in bench_data["media_artifacts"]
            if art["split"] == "m6_holdout"
        }
        dev_videos = load_dev_seed_manifest(dev_manifest_path)
        dev_annotations = load_dev_seed_annotations(dev_annotations_path)
        d0_summary = validate_dev_seed_dataset(
            dev_videos,
            dev_annotations,
            repo_root=root,
            verify_files_on_disk=True,
            holdout_source_group_ids=holdout_groups,
            holdout_video_ids=holdout_ids,
        )
        stage1_ms = int(round((time.perf_counter() - t1) * 1000))

        # Stage 2: F01-F04 fixture generation and independent self-check
        t2 = time.perf_counter()
        target_fixtures_dir = (
            fixtures_dir if fixtures_dir is not None else (root / DEFAULT_FIXTURES_DIR)
        )
        f01_f04_suite = build_f01_f04_suite(target_fixtures_dir, manifest_path=None)
        stage2_ms = int(round((time.perf_counter() - t2) * 1000))

        # Stage 3: Real packet-level ffprobe timeline inspection on all 6 D0 dev videos
        t3 = time.perf_counter()
        per_video_probe: list[dict[str, Any]] = []
        total_media_duration_ms = 0
        longest_clip_duration_ms = 0
        for vid in dev_videos:
            media_path = root / str(vid["relative_path"])
            probe_obs = _probe_video_packets_ffprobe(media_path)
            dur_ms = int(vid["duration_ms"])
            total_media_duration_ms += dur_ms
            longest_clip_duration_ms = max(longest_clip_duration_ms, dur_ms)
            rtf = round(probe_obs["probe_duration_ms"] / dur_ms, 5) if dur_ms > 0 else 0.0
            per_video_probe.append(
                {
                    "video_id": vid["video_id"],
                    "media_duration_ms": dur_ms,
                    "video_codec": probe_obs["video_codec"],
                    "video_packet_count": probe_obs["video_packet_count"],
                    "probe_duration_ms": probe_obs["probe_duration_ms"],
                    "demux_probe_rtf": rtf,
                }
            )
        stage3_ms = int(round((time.perf_counter() - t3) * 1000))
        aggregate_probe_rtf = (
            round(stage3_ms / total_media_duration_ms, 5) if total_media_duration_ms > 0 else 0.0
        )

        # Stage 4: Provider contract offline boundary + M0-A1 real frame serialization & body hashes
        t4 = time.perf_counter()
        provider_cfg_path = root / DEFAULT_PROVIDER_CONFIG_PATH
        capabilities_path = root / DEFAULT_CAPABILITIES_OUT
        provider_cfg = ProviderContractConfig.from_yaml(provider_cfg_path)
        boundary_suite = run_offline_boundary_suite(provider_cfg)
        m0_a1_eval = evaluate_m0_a1_extracted_samples(
            config=provider_cfg,
            manifest_path=benchmark_manifest_path,
            repo_root=root,
        )
        stage4_ms = int(round((time.perf_counter() - t4) * 1000))

        # Gather exact request body hashes from Stage 4
        boundary_hashes = [
            {
                "case_id": case["case_id"],
                "image_count": case["image_count"],
                "max_side_px": case["max_side_px"],
                "serialized_body_bytes": case["actual_serialized_body_bytes"],
                "serialized_body_sha256": case["serialized_request_body_sha256"],
                "allowed_by_documented_contract": case["allowed_by_documented_contract"],
            }
            for case in boundary_suite["cases"]
        ]
        roi_hashes = [
            {
                "sample_id": item["sample_id"],
                "benchmark": item["benchmark"],
                "frame_sha256": item["frame_sha256"],
                "strategy_a_body_bytes": item["comparison"]["strategy_a_original_high"][
                    "serialized_request_body_bytes"
                ],
                "strategy_a_body_sha256": item["comparison"]["strategy_a_original_high"][
                    "serialized_request_body_sha256"
                ],
                "strategy_b_body_bytes": item["comparison"]["strategy_b_960_plus_grid_roi"][
                    "serialized_request_body_bytes"
                ],
                "strategy_b_body_sha256": item["comparison"]["strategy_b_960_plus_grid_roi"][
                    "serialized_request_body_sha256"
                ],
            }
            for item in m0_a1_eval["roi_comparisons"]
        ]
        order_hashes = [
            {
                "sample_id": item["sample_id"],
                "benchmark": item["benchmark"],
                "frame_count": item["frame_count"],
                "serialized_body_bytes": item["serialized_request_body_bytes"],
                "serialized_body_sha256": item["serialized_request_body_sha256"],
                "pts_strictly_increasing": item["pts_strictly_increasing"],
            }
            for item in m0_a1_eval["multi_image_order_sequences"]
        ]
        combined_req_hash = _sha256_canonical_json(
            {
                "offline_boundary_cases": boundary_hashes,
                "m0_a1_roi_requests": roi_hashes,
                "m0_a1_multi_image_order_requests": order_hashes,
            }
        )

        # Gather exact input hashes
        dev_video_hashes = [
            {
                "video_id": vid["video_id"],
                "benchmark": vid["benchmark"],
                "source_group_id": vid["source_group_id"],
                "relative_path": vid["relative_path"],
                "byte_size": vid["byte_size"],
                "sha256": vid["sha256"],
                "duration_ms": vid["duration_ms"],
                "video_codec": vid["video_codec"],
                "has_audio": vid["has_audio"],
            }
            for vid in dev_videos
        ]
        m0_a1_frame_digests = sorted(
            (str(fr["relative_path"]), str(fr["sha256"]))
            for sample in bench_data.get("m0_a1_extracted_samples", [])
            for fr in sample.get("frames", [])
        )
        m0_a1_frames_bundle_sha256 = _sha256_canonical_json(m0_a1_frame_digests)

        dev_manifest_sha = _sha256_file(dev_manifest_path)
        dev_annotations_sha = _sha256_file(dev_annotations_path)
        benchmark_manifest_sha = _sha256_file(benchmark_manifest_path)
        fixture_manifest_sha = _sha256_file(fixture_manifest_path)
        combined_inputs_sha256 = _sha256_canonical_json(
            {
                "dev_seed_manifest_sha256": dev_manifest_sha,
                "dev_seed_annotations_sha256": dev_annotations_sha,
                "benchmark_subset_manifest_sha256": benchmark_manifest_sha,
                "f01_f04_fixture_manifest_sha256": fixture_manifest_sha,
                "m0_a1_frames_bundle_sha256": m0_a1_frames_bundle_sha256,
                "dev_seed_videos": dev_video_hashes,
            }
        )

        # Peak memory & total elapsed time
        _, tracemalloc_peak_bytes = tracemalloc.get_traced_memory()
        peak_rss_bytes, rss_method = measure_process_peak_rss_bytes()
        total_run_duration_ms = int(round((time.perf_counter() - run_start) * 1000))

        # Capabilities summary
        caps_data = json.loads(capabilities_path.read_text(encoding="utf-8"))
        raw_caps = caps_data.get("capabilities", [])
        cap_entries = raw_caps.values() if isinstance(raw_caps, dict) else raw_caps
        runtime_verified_count = sum(
            1
            for cap in cap_entries
            if isinstance(cap, dict) and cap.get("status") == "runtime_verified"
        )

        code_info = collect_code_version_info(root)
        code_info["run_id"] = run_id

        deps_info = _collect_dependencies_info(root)
        report: dict[str, Any] = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "run_id": run_id,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "execution_mode": "offline",
            "code_version": code_info,
            "machine": collect_sanitized_machine_info(),
            "dependencies": deps_info,
            "model_and_sdk": {
                "provider": provider_cfg.provider,
                "api_family": provider_cfg.api_family,
                "base_url": provider_cfg.base_url,
                "configured_model_id": provider_cfg.model_id,
                "documented_model_version": provider_cfg.documented_model_version,
                "provider_config_sha256": _sha256_file(provider_cfg_path),
                "capabilities_sha256": _sha256_file(capabilities_path),
                "sdk_name": "openai",
                "sdk_version": deps_info["packages"]["openai"],
                "httpx_version": deps_info["packages"]["httpx"],
                "runtime_verified_capability_count": runtime_verified_count,
            },
            "input_hashes": {
                "run_id": run_id,
                "dev_seed_manifest_sha256": dev_manifest_sha,
                "dev_seed_annotations_sha256": dev_annotations_sha,
                "benchmark_subset_manifest_sha256": benchmark_manifest_sha,
                "f01_f04_fixture_manifest_sha256": fixture_manifest_sha,
                "m0_a1_frames_count": len(m0_a1_frame_digests),
                "m0_a1_frames_bundle_sha256": m0_a1_frames_bundle_sha256,
                "dev_seed_videos": dev_video_hashes,
                "combined_inputs_sha256": combined_inputs_sha256,
            },
            "request_body_hashes": {
                "run_id": run_id,
                "offline_boundary_cases": boundary_hashes,
                "m0_a1_roi_requests": roi_hashes,
                "m0_a1_multi_image_order_requests": order_hashes,
                "combined_request_bodies_sha256": combined_req_hash,
            },
            "stage_timings_and_rtf": {
                "run_id": run_id,
                "total_run_duration_ms": total_run_duration_ms,
                "extrapolates_to_10min_full_pipeline": False,
                "measured_scope_note": (
                    "Measures offline D0 disk SHA-256 validation, F01-F04 FFmpeg fixture "
                    "generation/self-check, packet-level ffprobe demux across all 6 D0 dev videos "
                    "(total 888.8s, longest clip 507.1s), and M0-A1 request body serialization. "
                    "Full ASR + Vision + FTS indexing RTF is not_tested_yet and MUST NOT be "
                    "extrapolated from offline ffprobe/serialization timings."
                ),
                "stages": {
                    "d0_manifest_validation": {
                        "duration_ms": stage1_ms,
                        "verified_videos": d0_summary.verified_on_disk_files,
                        "question_count": d0_summary.question_count,
                        "answerable_count": d0_summary.answerable_count,
                        "not_answerable_count": d0_summary.not_answerable_count,
                    },
                    "f01_f04_fixture_self_check": {
                        "duration_ms": stage2_ms,
                        "passed_fixtures": sum(
                            1 for f in f01_f04_suite["fixtures"].values() if f["passed"]
                        ),
                        "fixture_ids": sorted(f01_f04_suite["fixtures"].keys()),
                    },
                    "dev_seed_media_probe": {
                        "duration_ms": stage3_ms,
                        "probe_mode": "ffprobe_packet_pts_demux",
                        "total_media_duration_ms": total_media_duration_ms,
                        "longest_clip_duration_ms": longest_clip_duration_ms,
                        "aggregate_probe_rtf": aggregate_probe_rtf,
                        "per_video": per_video_probe,
                    },
                    "provider_probe_offline_serialization": {
                        "duration_ms": stage4_ms,
                        "boundary_case_count": len(boundary_hashes),
                        "m0_a1_roi_case_count": len(roi_hashes),
                        "m0_a1_order_case_count": len(order_hashes),
                        "verified_real_frames": m0_a1_eval["verified_real_frame_count"],
                    },
                },
            },
            "peak_memory": {
                "run_id": run_id,
                "measurement_method": f"{rss_method}+python_tracemalloc",
                "process_peak_rss_bytes": peak_rss_bytes,
                "process_peak_rss_mib": round(peak_rss_bytes / (1024 * 1024), 2),
                "python_tracemalloc_peak_bytes": int(tracemalloc_peak_bytes),
                "python_tracemalloc_peak_mib": round(tracemalloc_peak_bytes / (1024 * 1024), 2),
                "note": (
                    "process_peak_rss_bytes captures the Python baseline process peak working set "
                    "during this correlated run; external ffmpeg/ffprobe child subprocesses run "
                    "in separate OS address spaces."
                ),
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
                "note": (
                    "Zero external provider calls occurred in this offline baseline run "
                    "(confirmed $0.00 cloud spend). Live DeepSeek token usage, latency, and "
                    "small-text quality remain not_tested_live pending API credentials and "
                    "explicit M0.0 cloud_egress_authorized=true."
                ),
            },
            "d1_annotation_plan": build_d1_annotation_plan(),
            "decision_and_gates": {
                "adr_path": "docs/decisions/ADR-001.md",
                "verdict": "pause_pending_live_verification",
                "m0_a1_status": "offline_verified_pending_live_credentials_and_egress_consent",
                "m0_a2_status": "passed_offline",
                "m0_a3_status": "baseline_recorded_pending_live_usage_and_independent_review",
                "handoff_audit": {
                    "PR-M0.0": "pending_independent_review",
                    "PR-M0.1": (
                        "pending_independent_review "
                        "(disclosed_process_deviation: no pre-implementation frozen Step A commit)"
                    ),
                    "PR-M0.2": "pending_independent_review (Step A -> Step B recorded)",
                    "PR-M0.3": "pending_independent_review (Step A -> Step B recorded)",
                },
            },
        }
    finally:
        if not was_tracing:
            tracemalloc.stop()

    sanitized_report = sanitize_record(report)
    if not isinstance(sanitized_report, dict):
        raise BaselineReportError("Sanitized baseline report must be a dict")
    validate_baseline_report(sanitized_report)

    if output_path is not None:
        out = output_path if output_path.is_absolute() else (root / output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(sanitized_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return sanitized_report


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint to run the correlated M0-A3 baseline and write baseline_report.json."""
    parser = argparse.ArgumentParser(description="Run M0-A3 correlated offline baseline.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_BASELINE_REPORT_PATH,
        help="Path to write baseline_report.json.",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=None,
        help="Gitignored directory for F01-F04 synthetic media outputs.",
    )
    args = parser.parse_args(argv)
    report = run_m0_baseline(output_path=args.output, fixtures_dir=args.fixtures_dir)
    print(
        f"M0-A3 baseline recorded: run_id={report['run_id']}, "
        f"duration_ms={report['stage_timings_and_rtf']['total_run_duration_ms']}, "
        f"peak_rss_mib={report['peak_memory']['process_peak_rss_mib']}, "
        f"cost_status={report['usage_and_cost']['cost_status']} -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
