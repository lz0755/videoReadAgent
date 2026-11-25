"""M0 / M1.5 / M6 Benchmark Subset Curator & M0-A1 Sample Extractor.

Curates a controlled, strictly <= 20 GB subset across three public benchmarks:
- MME-VideoOCR (DogNeverSleep/MME-VideoOCR_Dataset)
- VideoGUI (showlab/VideoGUI + official HuggingFace VideoGUI/* repos)
- Video-MME v2 (MME-Benchmarks/Video-MME-v2)

Design guarantees:
- Hard total size budget enforcement (<= 20 GB / 20_000_000_000 bytes).
- Selective HTTP Range extraction from multi-GB HuggingFace ZIP archives
  (never downloads the full 17 GB Video.zip or 105 GB Video-MME-v2 zips).
- Strict source-group / original-ID / URL / task-ID isolation between
  dev splits (m0_dev, m1_5_slice, d1_dev_extra) and m6_holdout.
- Mandatory authorization/license metadata and SHA-256 content hashes for
  every media artifact.
- Real ffprobe/ffmpeg inspection and actual PTS preservation for extracted
  M0-A1 frames (never fabricates timestamps from frame_index/fps or requested_ms).
- Sanitized repo-tracked manifest (zero private absolute paths, zero raw frames,
  zero holdout ground-truth answers).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from PIL import Image

from experiments.probe_gate import sanitize_record

# Strict decimal 20 GB cap (stricter than 20 GiB = 21_474_836_480 bytes).
MAX_DATASET_TOTAL_BYTES: int = 20_000_000_000

SplitName = Literal["m0_dev", "d1_dev_extra", "m6_holdout"]
BenchmarkName = Literal["mme_videoocr", "videogui", "video_mme_v2"]

DEV_SPLITS: frozenset[str] = frozenset({"m0_dev", "d1_dev_extra"})
HOLDOUT_SPLITS: frozenset[str] = frozenset({"m6_holdout"})
VALID_SPLITS: frozenset[str] = DEV_SPLITS | HOLDOUT_SPLITS

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_PATH_RE = re.compile(
    r"([A-Za-z]:\\Users\\|/Users/[^/\s]+|/home/[^/\s]+)",
    re.IGNORECASE,
)

DEFAULT_MANIFEST_OUT = Path("experiments/manifests/benchmark_subset_manifest.json")
DEFAULT_LOCAL_DATA_ROOT = Path("data/datasets")


class DatasetBudgetError(ValueError):
    """Raised when selected or downloaded dataset bytes exceed the 20 GB cap."""


class DatasetIsolationError(ValueError):
    """Raised when dev and M6 holdout splits share any source group, ID, URL, or task."""


class DatasetIntegrityError(ValueError):
    """Raised when authorization, SHA-256, schema, or sanitization checks fail."""


@dataclass(frozen=True, slots=True)
class BenchmarkAuthorization:
    """Recorded authorization / license scope for a benchmark source."""

    benchmark: BenchmarkName
    source_repo_url: str
    license_name: str
    commercial_use_allowed: bool
    redistribution_allowed: bool
    usage_scope: str
    recorded_at_utc: str
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "source_repo_url": self.source_repo_url,
            "license_name": self.license_name,
            "commercial_use_allowed": self.commercial_use_allowed,
            "redistribution_allowed": self.redistribution_allowed,
            "usage_scope": self.usage_scope,
            "recorded_at_utc": self.recorded_at_utc,
            "notes": self.notes,
        }


BENCHMARK_AUTHORIZATIONS: dict[BenchmarkName, BenchmarkAuthorization] = {
    "mme_videoocr": BenchmarkAuthorization(
        benchmark="mme_videoocr",
        source_repo_url="https://huggingface.co/datasets/DogNeverSleep/MME-VideoOCR_Dataset",
        license_name="CC-BY-NC-4.0",
        commercial_use_allowed=False,
        redistribution_allowed=False,
        usage_scope="local_non_commercial_research_eval_only",
        recorded_at_utc="2026-11-25T00:00:00+00:00",
        notes=(
            "Dataset card specifies CC-BY-NC 4.0, academic research only; original video "
            "copyright remains with respective owners; stored locally under gitignored data/."
        ),
    ),
    "videogui": BenchmarkAuthorization(
        benchmark="videogui",
        source_repo_url="https://github.com/showlab/VideoGUI",
        license_name="academic_benchmark_unspecified_osi_license",
        commercial_use_allowed=False,
        redistribution_allowed=False,
        usage_scope="local_non_commercial_research_eval_only",
        recorded_at_utc="2026-11-25T00:00:00+00:00",
        notes=(
            "NeurIPS 2024 D&B public academic benchmark (showlab/VideoGUI & official "
            "HuggingFace org VideoGUI/*). GitHub repo has no OSI LICENSE file; restricted "
            "to local non-commercial evaluation and stored under gitignored data/."
        ),
    ),
    "video_mme_v2": BenchmarkAuthorization(
        benchmark="video_mme_v2",
        source_repo_url="https://huggingface.co/datasets/MME-Benchmarks/Video-MME-v2",
        license_name="academic_benchmark_public_research",
        commercial_use_allowed=False,
        redistribution_allowed=False,
        usage_scope="local_non_commercial_research_eval_only",
        recorded_at_utc="2026-11-25T00:00:00+00:00",
        notes=(
            "Public academic video benchmark on HuggingFace (MME-Benchmarks/Video-MME-v2). "
            "No redistribution; stored locally under gitignored data/. Cloud egress to any "
            "third-party LLM requires explicit M0.0 cloud_egress_authorized consent."
        ),
    ),
}


def compute_file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream-compute SHA-256 hex digest of a local file without full memory load."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def pixel_to_grid_region(x: int, y: int, width: int, height: int) -> str:
    """Map a pixel coordinate (x, y) on an image (width, height) into 3x3 grid r1c1..r3c3."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Image dimensions must be positive, got {width}x{height}")
    clamped_x = max(0, min(width - 1, x))
    clamped_y = max(0, min(height - 1, y))
    col = min(2, int((clamped_x * 3) // width)) + 1
    row = min(2, int((clamped_y * 3) // height)) + 1
    return f"r{row}c{col}"


def enforce_dataset_size_budget(
    total_bytes: int,
    *,
    max_total_bytes: int = MAX_DATASET_TOTAL_BYTES,
    context: str = "dataset_subset",
) -> None:
    """Fail fast if total dataset bytes are negative or exceed the 20 GB hard cap."""
    if total_bytes < 0:
        raise DatasetBudgetError(f"{context}: byte count cannot be negative ({total_bytes})")
    if total_bytes > max_total_bytes:
        raise DatasetBudgetError(
            f"{context}: total_bytes={total_bytes} ({total_bytes / 1e9:.3f} GB) exceeds "
            f"hard cap max_total_bytes={max_total_bytes} ({max_total_bytes / 1e9:.3f} GB)"
        )


def to_repo_relative_posix(path: Path, *, repo_root: Path | None = None) -> str:
    """Convert a local path under repo_root to a sanitized POSIX relative path."""
    root = (repo_root or Path.cwd()).resolve()
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise DatasetIntegrityError(
            f"Path escapes repository root and cannot be recorded safely: {path.name}"
        ) from exc
    return rel.as_posix()


class HTTPRangeZipReader(io.RawIOBase):
    """Seekable read-only file backed by HTTP Range requests for remote ZIP extraction."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        max_retries: int = 5,
        chunk_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._url = url
        self._max_retries = max_retries
        self._chunk_bytes = chunk_bytes
        self._size = self._fetch_content_length()
        self._pos = 0
        self._buf_start = 0
        self._buf = b""

    def _fetch_content_length(self) -> int:
        for attempt in range(self._max_retries):
            try:
                resp = self._client.head(self._url, follow_redirects=True)
                resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                if not content_length:
                    raise RuntimeError(f"Missing Content-Length header for {self._url}")
                return int(content_length)
            except httpx.HTTPError:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("Unreachable")

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"Unsupported whence: {whence}")
        self._pos = max(0, min(self._size, target))
        return self._pos

    def _fetch_range(self, start: int, end: int) -> bytes:
        for attempt in range(self._max_retries):
            try:
                resp = self._client.get(
                    self._url,
                    headers={"Range": f"bytes={start}-{end}"},
                    follow_redirects=True,
                )
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
        return b""

    def read(self, size: int = -1) -> bytes:
        if self._pos >= self._size:
            return b""
        want = (
            (self._size - self._pos)
            if (size is None or size < 0)
            else min(size, self._size - self._pos)
        )
        # Serve from 4 MiB read-ahead buffer whenever covered.
        buf_end = self._buf_start + len(self._buf)
        if self._buf_start <= self._pos and self._pos + want <= buf_end:
            rel = self._pos - self._buf_start
            out = self._buf[rel : rel + want]
            self._pos += len(out)
            return out

        fetch_len = max(want, self._chunk_bytes)
        end = min(self._size - 1, self._pos + fetch_len - 1)
        data = self._fetch_range(self._pos, end)
        self._buf_start = self._pos
        self._buf = data
        out = data[:want]
        self._pos += len(out)
        return out


def _parse_time_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 1000.0))
    except TypeError, ValueError:
        return None


def _parse_fps(raw: Any) -> float | None:
    if not raw or raw == "0/0":
        return None
    try:
        return round(float(Fraction(str(raw))), 3)
    except ValueError, ZeroDivisionError:
        return None


def probe_video_with_ffprobe(video_path: Path) -> dict[str, Any]:
    """Inspect real video metadata using ffprobe (integer ms duration/start_time, codec, audio)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video_path.name}: {proc.stderr.strip()}")
    parsed = json.loads(proc.stdout)
    streams = parsed.get("streams", [])
    fmt = parsed.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise RuntimeError(f"No video stream detected in {video_path.name}")

    duration_ms = _parse_time_ms(fmt.get("duration") or video_stream.get("duration"))
    start_time_ms = _parse_time_ms(video_stream.get("start_time") or fmt.get("start_time"))
    return {
        "duration_ms": duration_ms,
        "start_time_ms": start_time_ms,
        "video_codec": str(video_stream.get("codec_name", "unknown")),
        "pix_fmt": str(video_stream.get("pix_fmt", "unknown")),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "time_base": str(video_stream.get("time_base", "")),
        "r_frame_rate": _parse_fps(video_stream.get("r_frame_rate")),
        "avg_frame_rate": _parse_fps(video_stream.get("avg_frame_rate")),
        "has_audio": audio_stream is not None,
        "audio_codec": str(audio_stream.get("codec_name")) if audio_stream else None,
        "p0_h264_native": str(video_stream.get("codec_name", "")).lower() == "h264",
    }


def extract_frame_with_real_pts(
    *,
    video_path: Path,
    requested_ms: int,
    output_png: Path,
    video_duration_ms: int | None = None,
) -> dict[str, Any]:
    """Extract a real video frame via ffmpeg and record its actual ffprobe PTS/time_base.

    Never substitutes requested_ms or frame_index/fps for actual_pts_ms.
    """
    if requested_ms < 0:
        raise ValueError(f"requested_ms must be >= 0, got {requested_ms}")
    if video_duration_ms is not None and video_duration_ms > 0:
        clamped_ms = min(requested_ms, max(0, video_duration_ms - 500))
    else:
        clamped_ms = requested_ms

    win_start_sec = max(0.0, (clamped_ms - 1000) / 1000.0)
    win_end_sec = (clamped_ms + 2500) / 1000.0

    # 1. Inspect actual decoded frame PTS in a tight window around clamped_ms.
    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        f"{win_start_sec:.3f}%{win_end_sec:.3f}",
        "-show_entries",
        "stream=time_base,start_time:frame=pts,pts_time,best_effort_timestamp,best_effort_timestamp_time",
        "-of",
        "json",
        str(video_path),
    ]
    proc = subprocess.run(probe_cmd, capture_output=True, text=True, check=False, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe frame PTS query failed on {video_path.name}: {proc.stderr}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    time_base = str(streams[0].get("time_base", "")) if streams else ""
    origin_ms = _parse_time_ms(streams[0].get("start_time")) if streams else None
    frames = data.get("frames", [])

    candidates: list[tuple[int, int, int | None]] = []
    for fr in frames:
        pts_time_raw = fr.get("pts_time") or fr.get("best_effort_timestamp_time")
        pts_raw = fr.get("pts") if fr.get("pts") is not None else fr.get("best_effort_timestamp")
        pts_ms = _parse_time_ms(pts_time_raw)
        if pts_ms is not None:
            pts_int = int(pts_raw) if pts_raw is not None else None
            candidates.append((abs(pts_ms - clamped_ms), pts_ms, pts_int))

    if not candidates:
        raise RuntimeError(
            f"No decoded video frames with valid PTS near {clamped_ms}ms in {video_path.name}"
        )
    # Prefer nearest frame at or after clamped_ms, else overall nearest.
    at_or_after = [c for c in candidates if c[1] >= clamped_ms]
    pool = at_or_after if at_or_after else candidates
    chosen = min(pool, key=lambda x: x[0])
    _, actual_pts_ms, actual_pts = chosen

    # 2. Extract exact frame at actual_pts_ms using accurate seek.
    output_png.parent.mkdir(parents=True, exist_ok=True)
    seek_sec = actual_pts_ms / 1000.0
    extract_cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{seek_sec:.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        str(output_png),
    ]
    ext_proc = subprocess.run(extract_cmd, capture_output=True, text=True, check=False, timeout=60)
    if ext_proc.returncode != 0 or not output_png.exists():
        raise RuntimeError(
            f"ffmpeg extraction failed for {video_path.name} @ {actual_pts_ms}ms: {ext_proc.stderr}"
        )

    return {
        "requested_ms": int(requested_ms),
        "actual_pts_ms": int(actual_pts_ms),
        "actual_pts": actual_pts,
        "time_base": time_base,
        "origin_pts_ms": origin_ms,
        "byte_size": output_png.stat().st_size,
        "sha256": compute_file_sha256(output_png),
    }


def _scan_for_forbidden_content(obj: Any, *, path: str = "$") -> None:
    """Ensure sanitized manifests never leak private local paths or holdout ground truth."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key_lower = str(k).lower()
            if key_lower in {"holdout_answer", "holdout_answers", "ground_truth_answer"}:
                raise DatasetIntegrityError(
                    f"Forbidden holdout answer key {k!r} found in sanitized manifest at {path}"
                )
            _scan_for_forbidden_content(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _scan_for_forbidden_content(item, path=f"{path}[{idx}]")
    elif isinstance(obj, str) and _PRIVATE_PATH_RE.search(obj):
        raise DatasetIntegrityError(
            f"Private local filesystem path leaked in sanitized manifest at {path}"
        )


def validate_subset_manifest(
    manifest: Mapping[str, Any],
    *,
    max_total_bytes: int = MAX_DATASET_TOTAL_BYTES,
) -> None:
    """Validate all invariants of the curated M0/M1.5/M6 benchmark subset manifest."""
    _scan_for_forbidden_content(manifest)

    totals = manifest.get("totals")
    if not isinstance(totals, Mapping):
        raise DatasetIntegrityError("Manifest missing 'totals' object")

    declared_max = int(totals.get("max_total_bytes", -1))
    if declared_max <= 0 or declared_max > max_total_bytes:
        raise DatasetBudgetError(
            f"Manifest max_total_bytes={declared_max} must be in (0, {max_total_bytes}]"
        )

    artifacts = manifest.get("media_artifacts")
    if not isinstance(artifacts, Sequence) or not artifacts:
        raise DatasetIntegrityError("Manifest missing non-empty 'media_artifacts'")

    dev_groups: set[str] = set()
    holdout_groups: set[str] = set()
    dev_orig_ids: set[tuple[str, str]] = set()
    holdout_orig_ids: set[tuple[str, str]] = set()
    dev_urls: set[str] = set()
    holdout_urls: set[str] = set()
    dev_tasks: set[tuple[str, str]] = set()
    holdout_tasks: set[tuple[str, str]] = set()

    computed_bytes = 0
    artifact_ids: set[str] = set()
    dev_artifact_ids: set[str] = set()
    holdout_artifact_ids: set[str] = set()

    for idx, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            raise DatasetIntegrityError(f"media_artifacts[{idx}] is not a mapping")

        aid = str(item.get("artifact_id", "")).strip()
        if not aid or aid in artifact_ids:
            raise DatasetIntegrityError(f"Duplicate or empty artifact_id at index {idx}: {aid!r}")
        artifact_ids.add(aid)

        split = str(item.get("split", ""))
        if split not in VALID_SPLITS:
            raise DatasetIntegrityError(f"{aid}: invalid split {split!r}")

        benchmark = str(item.get("benchmark", ""))
        if benchmark not in BENCHMARK_AUTHORIZATIONS:
            raise DatasetIntegrityError(f"{aid}: unknown benchmark {benchmark!r}")

        byte_size = int(item.get("byte_size", -1))
        if byte_size <= 0:
            raise DatasetIntegrityError(f"{aid}: byte_size must be positive, got {byte_size}")
        computed_bytes += byte_size

        sha256 = str(item.get("sha256", ""))
        if not _SHA256_RE.match(sha256):
            raise DatasetIntegrityError(f"{aid}: invalid sha256 digest {sha256!r}")

        auth = item.get("authorization")
        if (
            not isinstance(auth, Mapping)
            or not auth.get("license_name")
            or not auth.get("usage_scope")
        ):
            raise DatasetIntegrityError(f"{aid}: missing mandatory authorization record")

        rel_path = str(item.get("relative_path", ""))
        if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
            raise DatasetIntegrityError(f"{aid}: invalid relative_path {rel_path!r}")

        source_group = str(item.get("source_group_id", "")).strip()
        orig_id = str(item.get("original_id", "")).strip()
        if not source_group or not orig_id:
            raise DatasetIntegrityError(f"{aid}: missing source_group_id or original_id")

        source_url = str(item.get("source_video_url") or "").strip()
        task_id = str(item.get("task_id") or "").strip()

        if split in DEV_SPLITS:
            dev_artifact_ids.add(aid)
            dev_groups.add(source_group)
            dev_orig_ids.add((benchmark, orig_id))
            if source_url:
                dev_urls.add(source_url)
            if task_id:
                dev_tasks.add((benchmark, task_id))
        else:
            holdout_artifact_ids.add(aid)
            holdout_groups.add(source_group)
            holdout_orig_ids.add((benchmark, orig_id))
            if source_url:
                holdout_urls.add(source_url)
            if task_id:
                holdout_tasks.add((benchmark, task_id))

    # Strict Dev vs Holdout isolation checks.
    if overlap := (dev_groups & holdout_groups):
        raise DatasetIsolationError(f"Dev/Holdout overlap in source_group_id: {sorted(overlap)}")
    if overlap_ids := (dev_orig_ids & holdout_orig_ids):
        raise DatasetIsolationError(f"Dev/Holdout overlap in original_id: {sorted(overlap_ids)}")
    if overlap_urls := (dev_urls & holdout_urls):
        raise DatasetIsolationError(
            f"Dev/Holdout overlap in source_video_url: {sorted(overlap_urls)}"
        )
    if overlap_tasks := (dev_tasks & holdout_tasks):
        raise DatasetIsolationError(f"Dev/Holdout overlap in task_id: {sorted(overlap_tasks)}")

    # Verify M1.5 slice draws strictly from dev artifacts, NEVER holdout.
    m1_5_ids = manifest.get("m1_5_slice_artifact_ids", [])
    if not isinstance(m1_5_ids, Sequence) or not m1_5_ids:
        raise DatasetIntegrityError("Manifest missing non-empty m1_5_slice_artifact_ids")
    for m_id in m1_5_ids:
        if m_id in holdout_artifact_ids:
            raise DatasetIsolationError(
                f"M1.5 slice must NEVER reference holdout artifact {m_id!r}"
            )
        if m_id not in dev_artifact_ids:
            raise DatasetIntegrityError(f"M1.5 slice references unknown dev artifact {m_id!r}")

    # Verify M0-A1 extracted samples derive ONLY from dev artifacts and account for frame bytes.
    m0_a1_samples = manifest.get("m0_a1_extracted_samples", [])
    if not isinstance(m0_a1_samples, Sequence) or not m0_a1_samples:
        raise DatasetIntegrityError("Manifest missing non-empty m0_a1_extracted_samples")
    m0_a1_bytes = 0
    for s_idx, sample in enumerate(m0_a1_samples):
        if not isinstance(sample, Mapping):
            raise DatasetIntegrityError(f"m0_a1_extracted_samples[{s_idx}] is not a mapping")
        parent_id = str(sample.get("parent_artifact_id", ""))
        if parent_id in holdout_artifact_ids:
            sid = sample.get("sample_id")
            raise DatasetIsolationError(
                f"M0-A1 extracted sample {sid!r} cannot derive from holdout {parent_id!r}"
            )
        if parent_id not in dev_artifact_ids:
            raise DatasetIntegrityError(
                f"M0-A1 extracted sample references unknown dev artifact {parent_id!r}"
            )
        for frame in sample.get("frames", []):
            f_bytes = int(frame.get("byte_size", -1))
            f_sha = str(frame.get("sha256", ""))
            if f_bytes <= 0 or not _SHA256_RE.match(f_sha):
                raise DatasetIntegrityError(
                    f"M0-A1 sample {sample.get('sample_id')!r} has invalid frame byte_size/sha256"
                )
            m0_a1_bytes += f_bytes

    aux_bytes = int(totals.get("auxiliary_metadata_bytes", 0))
    if aux_bytes < 0:
        raise DatasetBudgetError("auxiliary_metadata_bytes cannot be negative")
    expected_total_bytes = computed_bytes + m0_a1_bytes + aux_bytes
    declared_total_bytes = int(totals.get("total_bytes", -1))
    if declared_total_bytes != expected_total_bytes:
        raise DatasetIntegrityError(
            f"Manifest totals.total_bytes={declared_total_bytes} != computed {expected_total_bytes}"
        )
    enforce_dataset_size_budget(
        declared_total_bytes,
        max_total_bytes=max_total_bytes,
        context="benchmark_subset_manifest",
    )


def build_sanitized_manifest(
    *,
    media_artifacts: Sequence[Mapping[str, Any]],
    m1_5_slice_artifact_ids: Sequence[str],
    m0_a1_extracted_samples: Sequence[Mapping[str, Any]],
    auxiliary_metadata_bytes: int,
    max_total_bytes: int = MAX_DATASET_TOTAL_BYTES,
) -> dict[str, Any]:
    """Build, sanitize, and strictly validate the repo-tracked benchmark subset manifest."""
    media_bytes = sum(int(x["byte_size"]) for x in media_artifacts)
    m0_a1_bytes = sum(
        int(frame["byte_size"])
        for sample in m0_a1_extracted_samples
        for frame in sample.get("frames", [])
    )
    total_bytes = media_bytes + m0_a1_bytes + int(auxiliary_metadata_bytes)
    enforce_dataset_size_budget(total_bytes, max_total_bytes=max_total_bytes)

    by_benchmark: dict[str, dict[str, int]] = {}
    by_split: dict[str, dict[str, int]] = {}
    for item in media_artifacts:
        bm = str(item["benchmark"])
        sp = str(item["split"])
        bs = int(item["byte_size"])
        bm_entry = by_benchmark.setdefault(bm, {"artifact_count": 0, "video_count": 0, "bytes": 0})
        bm_entry["artifact_count"] += 1
        if item.get("media_kind") == "video":
            bm_entry["video_count"] += 1
        bm_entry["bytes"] += bs

        sp_entry = by_split.setdefault(sp, {"artifact_count": 0, "video_count": 0, "bytes": 0})
        sp_entry["artifact_count"] += 1
        if item.get("media_kind") == "video":
            sp_entry["video_count"] += 1
        sp_entry["bytes"] += bs

    manifest: dict[str, Any] = {
        "schema_version": "m0-benchmark-subset-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "policy": {
            "hard_size_cap_gb": 20.0,
            "hard_size_cap_bytes": max_total_bytes,
            "raw_media_gitignored_root": "data/datasets",
            "holdout_isolation_rule": (
                "m6_holdout artifacts and private answer keys are strictly disjoint by "
                "source_group_id, original_id, source_video_url, and task_id; never used "
                "for M0/M1.5/M4 tuning."
            ),
            "cloud_egress_note": (
                "Local benchmark curation does NOT authorize cloud LLM upload. Sending any "
                "extracted frame to DeepSeek requires explicit M0.0 cloud_egress_authorized=true."
            ),
        },
        "benchmarks": {k: v.to_dict() for k, v in BENCHMARK_AUTHORIZATIONS.items()},
        "totals": {
            "media_artifact_bytes": media_bytes,
            "m0_a1_extracted_bytes": m0_a1_bytes,
            "auxiliary_metadata_bytes": int(auxiliary_metadata_bytes),
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / 1e9, 4),
            "max_total_bytes": max_total_bytes,
            "within_20gb_budget": total_bytes <= max_total_bytes,
            "by_benchmark": by_benchmark,
            "by_split": by_split,
        },
        "m1_5_slice_artifact_ids": list(m1_5_slice_artifact_ids),
        "media_artifacts": [dict(x) for x in media_artifacts],
        "m0_a1_extracted_samples": [dict(x) for x in m0_a1_extracted_samples],
    }
    sanitized = cast(dict[str, Any], sanitize_record(manifest))
    validate_subset_manifest(sanitized, max_total_bytes=max_total_bytes)
    return sanitized


@dataclass(frozen=True, slots=True)
class VideoSelectionSpec:
    """Declarative specification for one selected video in the <=20 GB benchmark subset."""

    artifact_id: str
    benchmark: BenchmarkName
    split: SplitName
    original_id: str
    source_group_id: str
    role_summary: str
    remote_archive_url: str | None = None
    archive_member: str | None = None
    direct_url: str | None = None
    task_id: str | None = None
    in_m1_5_slice: bool = False


MME_VIDEOOCR_ZIP_URL = (
    "https://huggingface.co/datasets/DogNeverSleep/MME-VideoOCR_Dataset/resolve/main/Video.zip"
)
VMME_V2_BASE_URL = "https://huggingface.co/datasets/MME-Benchmarks/Video-MME-v2/resolve/main"
VIDEOGUI_HIGH_PLAN_BASE = (
    "https://huggingface.co/datasets/VideoGUI/VideoGUI-High-Plan/resolve/main/high_plan"
)

CURATED_VIDEO_SPECS: tuple[VideoSelectionSpec, ...] = (
    # ---------------- M0 / D0 Dev (6 videos) ----------------
    VideoSelectionSpec(
        artifact_id="vg_ae10_start",
        benchmark="videogui",
        split="m0_dev",
        original_id="AE_10/start.mp4",
        source_group_id="videogui:AE_10",
        task_id="AE_10",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/AE_10/start.mp4",
        role_summary="After Effects visual tutorial start clip (paired with Mid-Plan GUI steps)",
    ),
    VideoSelectionSpec(
        artifact_id="vg_pr10_start",
        benchmark="videogui",
        split="m0_dev",
        original_id="PR_10/start.mp4",
        source_group_id="videogui:PR_10",
        task_id="PR_10",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/PR_10/start.mp4",
        in_m1_5_slice=True,
        role_summary="Premiere Pro visual tutorial clip (M0 dev + M1.5 software tutorial slice)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_941",
        benchmark="mme_videoocr",
        split="m0_dev",
        original_id="941",
        source_group_id="mme_videoocr:941",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/941.mp4",
        in_m1_5_slice=True,
        role_summary="30s GUI dialog small-text OCR (3 Qs; M0 dev + M1.5 slice + M0-A1)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_750",
        benchmark="mme_videoocr",
        split="m0_dev",
        original_id="750",
        source_group_id="mme_videoocr:750",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/750.mp4",
        in_m1_5_slice=True,
        role_summary="60s subtitle-based video understanding (2 Qs; exact 60s M1.5 slice)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_001",
        benchmark="video_mme_v2",
        split="m0_dev",
        original_id="001",
        source_group_id="video_mme_v2:001",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/001.zip",
        archive_member="001.mp4",
        role_summary="Frames & Audio + Temporal Reasoning + Complex Plot (4 Qs + subtitles)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_087",
        benchmark="video_mme_v2",
        split="m0_dev",
        original_id="087",
        source_group_id="video_mme_v2:087",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/005.zip",
        archive_member="087.mp4",
        role_summary="Procedural knowledge acquisition ~178s (4 Qs + subtitles)",
    ),
    # ---------------- D1 Additional Dev (+10 videos -> 16 Dev total) ----------------
    VideoSelectionSpec(
        artifact_id="vg_ae11_start",
        benchmark="videogui",
        split="d1_dev_extra",
        original_id="AE_11/start.mp4",
        source_group_id="videogui:AE_11",
        task_id="AE_11",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/AE_11/start.mp4",
        role_summary="After Effects D1 extra dev clip (paired with Mid-Plan GUI steps)",
    ),
    VideoSelectionSpec(
        artifact_id="vg_cc11_start",
        benchmark="videogui",
        split="d1_dev_extra",
        original_id="CC_11/start.mp4",
        source_group_id="videogui:CC_11",
        task_id="CC_11",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/CC_11/start.mp4",
        role_summary="CapCut D1 extra dev clip (paired with Mid-Plan GUI steps)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_905",
        benchmark="mme_videoocr",
        split="d1_dev_extra",
        original_id="905",
        source_group_id="mme_videoocr:905",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/905.mp4",
        role_summary="22s GUI text-box typing & change detection (3 Qs; M0-A1 order sample)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_903",
        benchmark="mme_videoocr",
        split="d1_dev_extra",
        original_id="903",
        source_group_id="mme_videoocr:903",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/903.mp4",
        role_summary="30s Chinese small-text OCR counting (2 Qs; M0-A1 Chinese text sample)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_1243",
        benchmark="mme_videoocr",
        split="d1_dev_extra",
        original_id="1243",
        source_group_id="mme_videoocr:1243",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/1243.mp4",
        role_summary="37s IDE code-screen text reasoning (2 Qs; M0-A1 code small-text sample)",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_935",
        benchmark="mme_videoocr",
        split="d1_dev_extra",
        original_id="935",
        source_group_id="mme_videoocr:935",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/935.mp4",
        role_summary="30s temporal grounding + text recognition (3 Qs)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_011",
        benchmark="video_mme_v2",
        split="d1_dev_extra",
        original_id="011",
        source_group_id="video_mme_v2:011",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/001.zip",
        archive_member="011.mp4",
        role_summary="Frame-Only + Temporal Reasoning ~185s (4 Qs + subtitles)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_082",
        benchmark="video_mme_v2",
        split="d1_dev_extra",
        original_id="082",
        source_group_id="video_mme_v2:082",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/005.zip",
        archive_member="082.mp4",
        role_summary="Frame-Only + Visual Diagram Knowledge ~182s (4 Qs + subtitles)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_104",
        benchmark="video_mme_v2",
        split="d1_dev_extra",
        original_id="104",
        source_group_id="video_mme_v2:104",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/006.zip",
        archive_member="104.mp4",
        role_summary="Slow temporal Change progression ~176s (4 Qs + subtitles)",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_113",
        benchmark="video_mme_v2",
        split="d1_dev_extra",
        original_id="113",
        source_group_id="video_mme_v2:113",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/006.zip",
        archive_member="113.mp4",
        role_summary="Frame-Only small sign text reading ~367s (4 Qs + subtitles)",
    ),
    # ---------------- M6 Holdout (8 strictly disjoint videos) ----------------
    VideoSelectionSpec(
        artifact_id="vg_pr18_start",
        benchmark="videogui",
        split="m6_holdout",
        original_id="PR_18/start.mp4",
        source_group_id="videogui:PR_18",
        task_id="PR_18",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/PR_18/start.mp4",
        role_summary="M6 blind holdout: disjoint Premiere Pro task",
    ),
    VideoSelectionSpec(
        artifact_id="vg_cc12_start",
        benchmark="videogui",
        split="m6_holdout",
        original_id="CC_12/start.mp4",
        source_group_id="videogui:CC_12",
        task_id="CC_12",
        direct_url=f"{VIDEOGUI_HIGH_PLAN_BASE}/CC_12/start.mp4",
        role_summary="M6 blind holdout: disjoint CapCut task",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_914",
        benchmark="mme_videoocr",
        split="m6_holdout",
        original_id="914",
        source_group_id="mme_videoocr:914",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/914.mp4",
        role_summary="M6 blind holdout: disjoint 30s temporal grounding + URL OCR",
    ),
    VideoSelectionSpec(
        artifact_id="ocr_968",
        benchmark="mme_videoocr",
        split="m6_holdout",
        original_id="968",
        source_group_id="mme_videoocr:968",
        remote_archive_url=MME_VIDEOOCR_ZIP_URL,
        archive_member="Video/968.mp4",
        role_summary="M6 blind holdout: disjoint 21s change detection + text",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_084",
        benchmark="video_mme_v2",
        split="m6_holdout",
        original_id="084",
        source_group_id="video_mme_v2:084",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/005.zip",
        archive_member="084.mp4",
        role_summary="M6 blind holdout: Action & Motion + Physical World Reasoning",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_091",
        benchmark="video_mme_v2",
        split="m6_holdout",
        original_id="091",
        source_group_id="video_mme_v2:091",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/005.zip",
        archive_member="091.mp4",
        role_summary="M6 blind holdout: Frames & Audio + Temporal Reasoning",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_109",
        benchmark="video_mme_v2",
        split="m6_holdout",
        original_id="109",
        source_group_id="video_mme_v2:109",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/006.zip",
        archive_member="109.mp4",
        role_summary="M6 blind holdout: Action & Motion counting",
    ),
    VideoSelectionSpec(
        artifact_id="vmme_191",
        benchmark="video_mme_v2",
        split="m6_holdout",
        original_id="191",
        source_group_id="video_mme_v2:191",
        remote_archive_url=f"{VMME_V2_BASE_URL}/videos/010.zip",
        archive_member="191.mp4",
        role_summary="M6 blind holdout: Action & Motion posture/angle reasoning",
    ),
)


def _download_direct_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    running_bytes: int,
    max_total_bytes: int,
) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        sz = dest.stat().st_size
        enforce_dataset_size_budget(running_bytes + sz, max_total_bytes=max_total_bytes)
        return sz

    for attempt in range(5):
        try:
            with client.stream("GET", url, follow_redirects=True) as resp:
                resp.raise_for_status()
                cl = resp.headers.get("content-length")
                if cl is not None:
                    enforce_dataset_size_budget(
                        running_bytes + int(cl),
                        max_total_bytes=max_total_bytes,
                        context=f"pre-download {dest.name}",
                    )
                tmp = dest.with_suffix(dest.suffix + ".part")
                written = 0
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(1024 * 512):
                        written += len(chunk)
                        enforce_dataset_size_budget(
                            running_bytes + written,
                            max_total_bytes=max_total_bytes,
                            context=f"streaming {dest.name}",
                        )
                        fh.write(chunk)
                tmp.replace(dest)
                return written
        except httpx.HTTPError:
            if attempt == 4:
                raise
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Failed to download {url}")


def _extract_remote_zip_members(
    client: httpx.Client,
    zip_url: str,
    member_to_dest: Mapping[str, Path],
    *,
    running_bytes: int,
    max_total_bytes: int,
) -> int:
    """Selectively extract named members from a remote ZIP archive via HTTP Range requests."""
    pending = {m: p for m, p in member_to_dest.items() if not (p.exists() and p.stat().st_size > 0)}
    added = sum(
        p.stat().st_size for m, p in member_to_dest.items() if m not in pending and p.exists()
    )
    enforce_dataset_size_budget(running_bytes + added, max_total_bytes=max_total_bytes)
    if not pending:
        return added

    with zipfile.ZipFile(HTTPRangeZipReader(client, zip_url)) as zf:
        for member, dest in pending.items():
            info = zf.getinfo(member)
            enforce_dataset_size_budget(
                running_bytes + added + info.file_size,
                max_total_bytes=max_total_bytes,
                context=f"zip-member {member}",
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with zf.open(info, "r") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 512)
            tmp.replace(dest)
            added += dest.stat().st_size
            enforce_dataset_size_budget(running_bytes + added, max_total_bytes=max_total_bytes)
    return added


def curate_and_extract_benchmarks(
    *,
    data_root: Path = DEFAULT_LOCAL_DATA_ROOT,
    manifest_out: Path = DEFAULT_MANIFEST_OUT,
    max_total_bytes: int = MAX_DATASET_TOTAL_BYTES,
    cleanup_raw_parquet_cache: bool = True,
) -> dict[str, Any]:
    """Download selected <=20 GB benchmark subset, probe media, extract M0-A1 frames & manifest."""
    repo_root = Path.cwd().resolve()
    meta_cache = data_root / "_meta_cache"
    ocr_json_path = meta_cache / "mme_videoocr_dataset.json"
    vmme_json_path = meta_cache / "video_mme_v2_test.json"
    vmme_sub_zip_path = meta_cache / "video_mme_v2_subtitle.zip"
    vg_mid_json_path = meta_cache / "videogui_mid_plan_selected.json"

    for required in (ocr_json_path, vmme_json_path, vmme_sub_zip_path, vg_mid_json_path):
        if not required.exists():
            raise FileNotFoundError(
                f"Required metadata cache missing: {required}. Run metadata export first."
            )

    ocr_rows: list[dict[str, Any]] = json.loads(ocr_json_path.read_text(encoding="utf-8"))
    ocr_by_vid: dict[str, list[dict[str, Any]]] = {}
    for row in ocr_rows:
        ocr_by_vid.setdefault(str(row["video_index"]), []).append(row)

    vmme_rows: list[dict[str, Any]] = json.loads(vmme_json_path.read_text(encoding="utf-8"))
    vmme_by_vid: dict[str, list[dict[str, Any]]] = {}
    for row in vmme_rows:
        vmme_by_vid.setdefault(str(row["video_id"]), []).append(row)

    vg_mid_rows: list[dict[str, Any]] = json.loads(vg_mid_json_path.read_text(encoding="utf-8"))

    # Group remote ZIP extractions by archive URL so each central directory is read only once.
    zip_batches: dict[str, dict[str, Path]] = {}
    spec_to_local_path: dict[str, Path] = {}
    for spec in CURATED_VIDEO_SPECS:
        dest = data_root / spec.benchmark / spec.split / f"{spec.artifact_id}.mp4"
        spec_to_local_path[spec.artifact_id] = dest
        if spec.remote_archive_url and spec.archive_member:
            zip_batches.setdefault(spec.remote_archive_url, {})[spec.archive_member] = dest

    running_bytes = 0
    with httpx.Client(
        timeout=120.0,
        transport=httpx.HTTPTransport(retries=3),
    ) as client:
        # 1. Extract remote ZIP members via HTTP Range.
        for zip_url, member_map in zip_batches.items():
            running_bytes += _extract_remote_zip_members(
                client,
                zip_url,
                member_map,
                running_bytes=running_bytes,
                max_total_bytes=max_total_bytes,
            )

        # 2. Download direct VideoGUI High-Plan MP4 clips.
        for spec in CURATED_VIDEO_SPECS:
            if spec.direct_url:
                dest = spec_to_local_path[spec.artifact_id]
                running_bytes += _download_direct_file(
                    client,
                    spec.direct_url,
                    dest,
                    running_bytes=running_bytes,
                    max_total_bytes=max_total_bytes,
                )

    # 3. Extract matching Video-MME-v2 subtitle JSONLs for selected videos only.
    sub_out_dir = data_root / "video_mme_v2" / "subtitles"
    sub_out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(vmme_sub_zip_path) as zf:
        for spec in CURATED_VIDEO_SPECS:
            if spec.benchmark != "video_mme_v2":
                continue
            member = f"subtitle/{spec.original_id}.jsonl"
            sub_dest = sub_out_dir / f"{spec.artifact_id}_{spec.original_id}.jsonl"
            if member in zf.namelist():
                sub_dest.write_bytes(zf.read(member))

    # 4. Probe all 24 videos with ffprobe + compute SHA-256 + build artifact entries.
    media_artifacts: list[dict[str, Any]] = []
    video_probes: dict[str, dict[str, Any]] = {}
    dev_annotations: dict[str, list[dict[str, Any]]] = {
        "mme_videoocr": [],
        "video_mme_v2": [],
        "videogui_mid_plan": [],
    }
    holdout_private: dict[str, list[dict[str, Any]]] = {
        "mme_videoocr": [],
        "video_mme_v2": [],
        "videogui_mid_plan": [],
    }

    for spec in CURATED_VIDEO_SPECS:
        vpath = spec_to_local_path[spec.artifact_id]
        probe = probe_video_with_ffprobe(vpath)
        video_probes[spec.artifact_id] = probe
        vsize = vpath.stat().st_size
        vsha = compute_file_sha256(vpath)
        auth = BENCHMARK_AUTHORIZATIONS[spec.benchmark].to_dict()

        source_video_url: str | None = None
        question_count = 0
        task_categories: list[str] = []

        if spec.benchmark == "mme_videoocr":
            qrows = ocr_by_vid.get(spec.original_id, [])
            question_count = len(qrows)
            task_categories = sorted({str(r.get("task_type", "")) for r in qrows})
            target_bucket = (
                dev_annotations["mme_videoocr"]
                if spec.split in DEV_SPLITS
                else holdout_private["mme_videoocr"]
            )
            for qr in qrows:
                target_bucket.append(
                    {
                        "artifact_id": spec.artifact_id,
                        "split": spec.split,
                        "video_index": spec.original_id,
                        "question": qr.get("question"),
                        "answer": qr.get("answer"),
                        "task_type": qr.get("task_type"),
                        "task": qr.get("task"),
                        "eval_method": qr.get("eval_method"),
                    }
                )
        elif spec.benchmark == "video_mme_v2":
            qrows = vmme_by_vid.get(spec.original_id, [])
            question_count = len(qrows)
            if qrows:
                source_video_url = str(qrows[0].get("url", "")) or None
            task_categories = sorted({str(r.get("second_head", "")) for r in qrows})
            target_bucket = (
                dev_annotations["video_mme_v2"]
                if spec.split in DEV_SPLITS
                else holdout_private["video_mme_v2"]
            )
            for qr in qrows:
                target_bucket.append(
                    {
                        "artifact_id": spec.artifact_id,
                        "split": spec.split,
                        "video_id": spec.original_id,
                        "question_id": qr.get("question_id"),
                        "question": qr.get("question"),
                        "options": qr.get("options"),
                        "answer": qr.get("answer"),
                        "level": qr.get("level"),
                        "second_head": qr.get("second_head"),
                        "third_head": qr.get("third_head"),
                    }
                )

        media_artifacts.append(
            {
                "artifact_id": spec.artifact_id,
                "benchmark": spec.benchmark,
                "split": spec.split,
                "media_kind": "video",
                "original_id": spec.original_id,
                "source_group_id": spec.source_group_id,
                "task_id": spec.task_id,
                "source_video_url": source_video_url,
                "relative_path": to_repo_relative_posix(vpath, repo_root=repo_root),
                "byte_size": vsize,
                "sha256": vsha,
                "question_count": question_count,
                "task_categories": task_categories,
                "role_summary": spec.role_summary,
                "ffprobe": probe,
                "authorization": auth,
            }
        )

    # 5. Index extracted VideoGUI Mid-Plan high-res screenshots & separate dev/holdout annotations.
    vg_auth = BENCHMARK_AUTHORIZATIONS["videogui"].to_dict()
    vg_dev_screenshot_map: dict[str, tuple[str, Path, int, int, str]] = {}
    for rec in vg_mid_rows:
        split = str(rec["split"])
        tid = str(rec["task_id"])
        sub_id = int(rec["subtask_id"])
        app = str(rec["app"])
        bucket = (
            dev_annotations["videogui_mid_plan"]
            if split in DEV_SPLITS
            else holdout_private["videogui_mid_plan"]
        )
        bucket.append(rec)

        images_map = rec.get("images", {})
        if not isinstance(images_map, Mapping):
            continue
        for role_col, rel_str in images_map.items():
            img_path = repo_root / str(rel_str)
            if not img_path.exists():
                continue
            with Image.open(img_path) as im:
                w, h = im.size
            aid = f"vg_mid_{tid.lower()}_s{sub_id}_{'start' if 'start' in role_col else 'end'}"
            sha = compute_file_sha256(img_path)
            bsize = img_path.stat().st_size
            media_artifacts.append(
                {
                    "artifact_id": aid,
                    "benchmark": "videogui",
                    "split": split,
                    "media_kind": "screenshot",
                    "original_id": f"{tid}/subtask_{sub_id}/{role_col}",
                    "source_group_id": f"videogui:{tid}",
                    "task_id": tid,
                    "source_video_url": None,
                    "relative_path": to_repo_relative_posix(img_path, repo_root=repo_root),
                    "byte_size": bsize,
                    "sha256": sha,
                    "question_count": 1,
                    "task_categories": [f"gui_mid_plan:{app}"],
                    "role_summary": f"VideoGUI {app} {tid} subtask {sub_id} {role_col} ({w}x{h})",
                    "image_dimensions": [w, h],
                    "authorization": vg_auth,
                }
            )
            if (
                split in DEV_SPLITS
                and role_col == "screenshot_start"
                and tid not in vg_dev_screenshot_map
            ):
                vg_dev_screenshot_map[tid] = (aid, img_path, w, h, app)

    # 6. Extract M0-A1 real probe samples (STRICTLY from dev splits ONLY!).
    m0_a1_dir = data_root / "m0_a1_extracted"
    m0_a1_dir.mkdir(parents=True, exist_ok=True)
    m0_a1_samples: list[dict[str, Any]] = []

    # 6a. VideoGUI high-res software_tutorial frames (SD6, AE_10, PR_10, PS_10, DV_10)
    vg_roi_hints: dict[str, str] = {
        "SD6": "r2c1",
        "AE_10": "r2c2",
        "PR_10": "r2c2",
        "PS_10": "r2c2",
        "DV_10": "r2c2",
    }
    for tid in ("SD6", "AE_10", "PR_10", "PS_10", "DV_10"):
        if tid not in vg_dev_screenshot_map:
            continue
        parent_aid, src_path, w, h, app = vg_dev_screenshot_map[tid]
        out_png = m0_a1_dir / f"m0_a1_vg_{tid.lower()}_gui.png"
        with Image.open(src_path) as im:
            im.convert("RGB").save(out_png, format="PNG")
        m0_a1_samples.append(
            {
                "sample_id": f"m0_a1_vg_{tid.lower()}_gui",
                "benchmark": "videogui",
                "parent_artifact_id": parent_aid,
                "probe_purpose": "software_tutorial_high_vs_grid_roi",
                "app": app,
                "recommended_roi_region_id": vg_roi_hints.get(tid, "r2c2"),
                "frames": [
                    {
                        "frame_id": f"{tid.lower()}_gui_0",
                        "relative_path": to_repo_relative_posix(out_png, repo_root=repo_root),
                        "width": w,
                        "height": h,
                        "byte_size": out_png.stat().st_size,
                        "sha256": compute_file_sha256(out_png),
                        "timestamp_kind": "static_gui_keyframe",
                    }
                ],
            }
        )

    # 6b. Real video frames with actual ffprobe PTS from MME-VideoOCR & Video-MME v2 (dev ONLY!)
    video_extract_specs: tuple[tuple[str, str, str, str, tuple[int, ...], str], ...] = (
        (
            "m0_a1_ocr_941_dialog",
            "mme_videoocr",
            "ocr_941",
            "software_tutorial_high_vs_grid_roi",
            (5000, 15000, 25000),
            "r2c2",
        ),
        (
            "m0_a1_ocr_905_typing_order",
            "mme_videoocr",
            "ocr_905",
            "vision_multi_image_order",
            (2000, 8000, 14000, 20000),
            "r2c2",
        ),
        (
            "m0_a1_ocr_903_chinese_ocr",
            "mme_videoocr",
            "ocr_903",
            "chinese_small_text_ocr",
            (4000, 14000, 24000),
            "r2c2",
        ),
        (
            "m0_a1_ocr_1243_ide_code",
            "mme_videoocr",
            "ocr_1243",
            "software_tutorial_high_vs_grid_roi",
            (5000, 18000, 30000),
            "r2c2",
        ),
        (
            "m0_a1_vmme_001_order",
            "video_mme_v2",
            "vmme_001",
            "vision_multi_image_order",
            (30000, 120000, 240000, 360000),
            "r2c2",
        ),
        (
            "m0_a1_vmme_087_procedural",
            "video_mme_v2",
            "vmme_087",
            "vision_multi_image_order",
            (15000, 55000, 105000, 150000),
            "r2c2",
        ),
    )

    for sample_id, bm, parent_aid, purpose, req_times, roi_hint in video_extract_specs:
        vpath = spec_to_local_path[parent_aid]
        dur_ms = video_probes[parent_aid].get("duration_ms")
        extracted_frames: list[dict[str, Any]] = []
        for idx, req_ms in enumerate(req_times):
            out_png = m0_a1_dir / f"{sample_id}_f{idx}_{req_ms}ms.png"
            pts_info = extract_frame_with_real_pts(
                video_path=vpath,
                requested_ms=req_ms,
                output_png=out_png,
                video_duration_ms=dur_ms if isinstance(dur_ms, int) else None,
            )
            with Image.open(out_png) as im:
                fw, fh = im.size
            extracted_frames.append(
                {
                    "frame_id": f"{sample_id}_f{idx}",
                    "relative_path": to_repo_relative_posix(out_png, repo_root=repo_root),
                    "width": fw,
                    "height": fh,
                    "timestamp_kind": "ffprobe_actual_pts",
                    **pts_info,
                }
            )
        m0_a1_samples.append(
            {
                "sample_id": sample_id,
                "benchmark": bm,
                "parent_artifact_id": parent_aid,
                "probe_purpose": purpose,
                "recommended_roi_region_id": roi_hint,
                "frames": extracted_frames,
            }
        )

    # 7. Write local gitignored dev annotations & isolated holdout ground truth.
    local_manifests_dir = data_root / "manifests"
    local_manifests_dir.mkdir(parents=True, exist_ok=True)
    dev_ann_path = local_manifests_dir / "dev_annotations.json"
    dev_ann_path.write_text(
        json.dumps(dev_annotations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    holdout_priv_path = local_manifests_dir / "m6_holdout_private.json"
    holdout_priv_path.write_text(
        json.dumps(
            {
                "warning": "ISOLATED_M6_HOLDOUT_DO_NOT_USE_FOR_M0_M1_5_M4_TUNING",
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "annotations": holdout_private,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    m0_a1_index_path = m0_a1_dir / "m0_a1_samples.json"
    m0_a1_index_path.write_text(
        json.dumps(m0_a1_samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 8. Remove unselected temporary parquet cache so disk usage matches curated subset only.
    if cleanup_raw_parquet_cache and meta_cache.exists():
        shutil.rmtree(meta_cache, ignore_errors=True)

    # Compute exact auxiliary bytes (subtitles + local JSON manifests) for accurate <=20 GB total.
    aux_bytes = 0
    aux_paths = list(sub_out_dir.glob("*.jsonl")) + [
        dev_ann_path,
        holdout_priv_path,
        m0_a1_index_path,
    ]
    for p in aux_paths:
        if p.exists():
            aux_bytes += p.stat().st_size

    m1_5_ids = [s.artifact_id for s in CURATED_VIDEO_SPECS if s.in_m1_5_slice]
    manifest = build_sanitized_manifest(
        media_artifacts=media_artifacts,
        m1_5_slice_artifact_ids=m1_5_ids,
        m0_a1_extracted_samples=m0_a1_samples,
        auxiliary_metadata_bytes=aux_bytes,
        max_total_bytes=max_total_bytes,
    )

    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M0/M1.5/M6 <=20 GB Benchmark Subset Curator & M0-A1 Extractor."
    )
    parser.add_argument(
        "--execute-download",
        action="store_true",
        help="Actually download selected subset and extract M0-A1 frames into data/datasets/.",
    )
    parser.add_argument(
        "--validate-manifest",
        type=Path,
        default=None,
        help="Offline-validate an existing benchmark subset manifest JSON.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_LOCAL_DATA_ROOT,
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=DEFAULT_MANIFEST_OUT,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.validate_manifest is not None:
        raw = json.loads(args.validate_manifest.read_text(encoding="utf-8"))
        validate_subset_manifest(raw)
        totals = raw["totals"]
        print(
            f"Manifest valid: total={totals['total_gb']} GB "
            f"(cap={totals['max_total_bytes'] / 1e9:.1f} GB), "
            f"artifacts={len(raw['media_artifacts'])}, "
            f"m0_a1_samples={len(raw['m0_a1_extracted_samples'])}"
        )
        return 0

    if not args.execute_download:
        print(
            "Dry-run mode: pass --execute-download to fetch selected <=20 GB subset "
            "or --validate-manifest <path> to validate offline."
        )
        return 0

    manifest = curate_and_extract_benchmarks(
        data_root=args.data_root,
        manifest_out=args.manifest_out,
    )
    totals = manifest["totals"]
    print(
        f"Curated benchmark subset complete: total={totals['total_gb']} GB "
        f"(<=20.0 GB cap), media_artifacts={len(manifest['media_artifacts'])}, "
        f"m0_a1_samples={len(manifest['m0_a1_extracted_samples'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
