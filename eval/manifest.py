"""D0 dev seed manifest and 24-question annotation validation for M0-A2 / M0.3."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

D0_EXPECTED_VIDEO_COUNT: int = 6
D0_EXPECTED_QUESTIONS_PER_VIDEO: int = 4
D0_EXPECTED_TOTAL_QUESTIONS: int = 24
ALLOWED_ANSWERABILITY: frozenset[str] = frozenset({"answerable", "not_answerable"})
ALLOWED_EVIDENCE_MODALITIES: frozenset[str] = frozenset(
    {"visual", "audio", "visual_and_audio", "subtitle_and_audio", "absent_verified"}
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)")
REQUIRED_AUTH_FIELDS: tuple[str, ...] = (
    "source_repo_url",
    "license_name",
    "usage_scope",
    "commercial_use_allowed",
    "redistribution_allowed",
    "recorded_at_utc",
)


class DatasetManifestError(ValueError):
    """Raised when D0 manifest or annotations violate M0-A2 invariants."""


@dataclass(frozen=True)
class DevSeedValidationSummary:
    """Validated summary of the D0 6-video / 24-question development dataset."""

    video_count: int
    question_count: int
    answerable_count: int
    not_answerable_count: int
    m1_5_slice_video_ids: list[str]
    benchmarks: dict[str, int]
    verified_on_disk_files: int


def _is_strict_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_no_private_or_absolute_path(path_value: str, field_name: str) -> None:
    if not isinstance(path_value, str) or not path_value.strip():
        raise DatasetManifestError(f"{field_name} must be a non-empty relative path string")
    if PRIVATE_PATH_PATTERN.search(path_value):
        raise DatasetManifestError(f"Private path detected in {field_name}: {path_value}")
    if path_value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path_value):
        raise DatasetManifestError(f"Absolute path forbidden in {field_name}: {path_value}")
    if ".." in Path(path_value).parts:
        raise DatasetManifestError(f"Parent traversal forbidden in {field_name}: {path_value}")


def _compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_dev_seed_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Load JSONL entries from eval/manifests/dev_seed.jsonl."""
    if not manifest_path.is_file():
        raise DatasetManifestError(f"Dev seed manifest not found: {manifest_path}")
    entries: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetManifestError(
                f"Invalid JSON on line {line_no} of {manifest_path.name}: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise DatasetManifestError(f"Line {line_no} must be a JSON object")
        entries.append(obj)
    return entries


def load_dev_seed_annotations(annotations_path: Path) -> dict[str, Any]:
    """Load JSON question annotations from eval/annotations/dev_seed/."""
    if not annotations_path.is_file():
        raise DatasetManifestError(f"Dev seed annotations not found: {annotations_path}")
    try:
        parsed = json.loads(annotations_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetManifestError(f"Invalid annotations JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DatasetManifestError("Annotations JSON root must be an object")
    return parsed


def _validate_authorization(auth: Any, video_id: str) -> None:
    if not isinstance(auth, dict):
        raise DatasetManifestError(f"Video {video_id} is missing authorization object")
    for field in REQUIRED_AUTH_FIELDS:
        if field not in auth:
            raise DatasetManifestError(f"Video {video_id} authorization missing field: {field}")
        value = auth[field]
        if field in ("commercial_use_allowed", "redistribution_allowed"):
            if not isinstance(value, bool):
                raise DatasetManifestError(
                    f"Video {video_id} authorization.{field} must be boolean"
                )
        elif not isinstance(value, str) or not value.strip():
            raise DatasetManifestError(
                f"Video {video_id} authorization.{field} must be non-empty string"
            )


def _validate_evidence_refs(
    refs: Any,
    *,
    question_id: str,
    answerability: str,
    duration_ms: int,
) -> None:
    if not isinstance(refs, list) or len(refs) == 0:
        raise DatasetManifestError(
            f"Question {question_id} must provide at least one verifiable evidence_ref"
        )
    for idx, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise DatasetManifestError(f"Question {question_id} evidence_refs[{idx}] must be dict")
        start_ms = ref.get("start_ms")
        end_ms = ref.get("end_ms")
        if not _is_strict_int(start_ms) or not _is_strict_int(end_ms):
            raise DatasetManifestError(
                f"Question {question_id} evidence_refs[{idx}] start_ms/end_ms must be strict "
                f"integer milliseconds"
            )
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
            raise DatasetManifestError(
                f"Question {question_id} evidence_refs[{idx}] range [{start_ms}, {end_ms}) "
                f"is out of valid video duration [0, {duration_ms}]"
            )
        modality = ref.get("modality")
        if modality not in ALLOWED_EVIDENCE_MODALITIES:
            raise DatasetManifestError(
                f"Question {question_id} evidence_refs[{idx}] invalid modality: {modality}"
            )
        if answerability == "answerable" and modality == "absent_verified":
            raise DatasetManifestError(
                f"Answerable question {question_id} cannot use absent_verified modality"
            )
        basis = ref.get("basis")
        if not isinstance(basis, str) or not basis.strip():
            raise DatasetManifestError(
                f"Question {question_id} evidence_refs[{idx}] must include non-empty basis"
            )


def _verify_dev_seed_files(
    videos: list[dict[str, Any]], repo_root: Path | None, verify_files_on_disk: bool | None
) -> int:
    if repo_root is None:
        if verify_files_on_disk:
            raise DatasetManifestError("repo_root is required when verify_files_on_disk=True")
        return 0
    if verify_files_on_disk is False:
        return 0

    paths = [repo_root / str(entry["relative_path"]) for entry in videos]
    if verify_files_on_disk is None and not any(path.parent.exists() for path in paths):
        return 0

    for entry, path in zip(videos, paths, strict=True):
        video_id = entry["video_id"]
        if not path.is_file():
            raise DatasetManifestError(
                f"Video {video_id} file missing on disk: {entry['relative_path']}"
            )
        actual_size = path.stat().st_size
        if actual_size != entry["byte_size"]:
            raise DatasetManifestError(
                f"Video {video_id} byte_size mismatch: "
                f"expected {entry['byte_size']}, got {actual_size}"
            )
        actual_sha = _compute_file_sha256(path)
        if actual_sha != entry["sha256"]:
            raise DatasetManifestError(
                f"Video {video_id} sha256 hash mismatch: "
                f"expected {entry['sha256']}, got {actual_sha}"
            )
    return len(paths)


def validate_dev_seed_dataset(
    videos: list[dict[str, Any]],
    annotations: dict[str, Any],
    *,
    repo_root: Path | None = None,
    verify_files_on_disk: bool | None = None,
    holdout_source_group_ids: set[str] | None = None,
    holdout_video_ids: set[str] | None = None,
) -> DevSeedValidationSummary:
    """Validate 6 dev videos and 24 questions for M0-A2 compliance."""
    if len(videos) != D0_EXPECTED_VIDEO_COUNT:
        raise DatasetManifestError(
            f"D0 dev_seed requires exactly {D0_EXPECTED_VIDEO_COUNT} videos, got {len(videos)}"
        )

    holdout_groups = holdout_source_group_ids or set()
    holdout_ids = holdout_video_ids or set()

    seen_video_ids: set[str] = set()
    seen_source_groups: set[str] = set()
    duration_by_video: dict[str, int] = {}
    benchmarks: dict[str, int] = {}
    m1_5_slice_ids: list[str] = []

    for entry in videos:
        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise DatasetManifestError("Every video entry requires a non-empty video_id")
        if video_id in seen_video_ids:
            raise DatasetManifestError(f"Duplicate video_id detected: {video_id}")
        seen_video_ids.add(video_id)

        if video_id in holdout_ids:
            raise DatasetManifestError(f"Holdout video_id contaminated dev_seed: {video_id}")

        split = entry.get("split")
        if split != "m0_dev":
            raise DatasetManifestError(
                f"Video {video_id} must belong to split 'm0_dev', got {split}"
            )

        source_group_id = entry.get("source_group_id")
        if not isinstance(source_group_id, str) or not source_group_id.strip():
            raise DatasetManifestError(f"Video {video_id} missing source_group_id")
        if source_group_id in seen_source_groups:
            raise DatasetManifestError(
                f"Duplicate source_group_id detected in dev_seed: {source_group_id}"
            )
        seen_source_groups.add(source_group_id)

        if source_group_id in holdout_groups:
            raise DatasetManifestError(
                f"Holdout source_group_id contaminated dev_seed: {source_group_id}"
            )

        rel_path = entry.get("relative_path")
        _check_no_private_or_absolute_path(str(rel_path or ""), f"{video_id}.relative_path")

        ann_path = entry.get("annotations_path")
        _check_no_private_or_absolute_path(str(ann_path or ""), f"{video_id}.annotations_path")

        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise DatasetManifestError(f"Video {video_id} has missing or invalid sha256: {sha256}")

        byte_size = entry.get("byte_size")
        if not _is_strict_int(byte_size) or byte_size <= 0:
            raise DatasetManifestError(f"Video {video_id} byte_size must be positive int")

        duration_ms = entry.get("duration_ms")
        if not _is_strict_int(duration_ms) or duration_ms <= 0:
            raise DatasetManifestError(f"Video {video_id} duration_ms must be positive int")
        duration_by_video[video_id] = duration_ms

        _validate_authorization(entry.get("authorization"), video_id)

        benchmark = str(entry.get("benchmark", "")).strip()
        if not benchmark:
            raise DatasetManifestError(f"Video {video_id} missing benchmark name")
        benchmarks[benchmark] = benchmarks.get(benchmark, 0) + 1

        if entry.get("m1_5_slice") is True:
            m1_5_slice_ids.append(video_id)

    verified_on_disk = _verify_dev_seed_files(videos, repo_root, verify_files_on_disk)

    raw_annotations_text = json.dumps(annotations, ensure_ascii=False)
    if PRIVATE_PATH_PATTERN.search(raw_annotations_text):
        raise DatasetManifestError("Private absolute path detected inside annotations payload")

    questions = annotations.get("questions")
    if not isinstance(questions, list):
        raise DatasetManifestError("Annotations must contain a 'questions' list")
    if len(questions) != D0_EXPECTED_TOTAL_QUESTIONS:
        raise DatasetManifestError(
            f"D0 dev_seed requires exactly {D0_EXPECTED_TOTAL_QUESTIONS} questions, "
            f"got {len(questions)}"
        )

    seen_question_ids: set[str] = set()
    per_video_counts: dict[str, int] = {vid: 0 for vid in seen_video_ids}
    answerable_count = 0
    not_answerable_count = 0

    for q in questions:
        if not isinstance(q, dict):
            raise DatasetManifestError("Each question entry must be a JSON object")
        qid = q.get("question_id")
        if not isinstance(qid, str) or not qid.strip():
            raise DatasetManifestError("Every question requires a non-empty question_id")
        if qid in seen_question_ids:
            raise DatasetManifestError(f"Duplicate question_id: {qid}")
        seen_question_ids.add(qid)

        vid = q.get("video_id")
        if vid not in duration_by_video:
            raise DatasetManifestError(f"Question {qid} references unknown video_id: {vid}")
        per_video_counts[vid] += 1

        q_text = q.get("question_text")
        if not isinstance(q_text, str) or not q_text.strip():
            raise DatasetManifestError(f"Question {qid} missing question_text")

        verification_basis = q.get("verification_basis")
        if not isinstance(verification_basis, str) or not verification_basis.strip():
            raise DatasetManifestError(f"Question {qid} missing verification_basis")

        answerability = q.get("answerability")
        if answerability not in ALLOWED_ANSWERABILITY:
            raise DatasetManifestError(f"Question {qid} invalid answerability: {answerability}")

        _validate_evidence_refs(
            q.get("evidence_refs"),
            question_id=qid,
            answerability=str(answerability),
            duration_ms=duration_by_video[vid],
        )

        if answerability == "answerable":
            answerable_count += 1
            ref_answer = q.get("reference_answer")
            if not isinstance(ref_answer, str) or not ref_answer.strip():
                raise DatasetManifestError(
                    f"Answerable question {qid} must have non-empty reference_answer"
                )
        else:
            not_answerable_count += 1
            if q.get("reference_answer") is not None:
                raise DatasetManifestError(
                    f"not_answerable question {qid} must set reference_answer to null"
                )
            expected_gaps = q.get("expected_gaps")
            if (
                not isinstance(expected_gaps, list)
                or len(expected_gaps) == 0
                or not all(isinstance(g, str) and g.strip() for g in expected_gaps)
            ):
                raise DatasetManifestError(
                    f"not_answerable question {qid} must provide non-empty expected_gaps"
                )

    for vid, count in per_video_counts.items():
        if count != D0_EXPECTED_QUESTIONS_PER_VIDEO:
            raise DatasetManifestError(
                f"Video {vid} must have exactly {D0_EXPECTED_QUESTIONS_PER_VIDEO} questions, "
                f"got {count}"
            )

    return DevSeedValidationSummary(
        video_count=len(videos),
        question_count=len(questions),
        answerable_count=answerable_count,
        not_answerable_count=not_answerable_count,
        m1_5_slice_video_ids=m1_5_slice_ids,
        benchmarks=benchmarks,
        verified_on_disk_files=verified_on_disk,
    )


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate D0 dev seed manifest and 24 questions.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/manifests/dev_seed.jsonl"),
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("eval/annotations/dev_seed/dev_seed_questions.json"),
    )
    parser.add_argument(
        "--benchmark-manifest",
        type=Path,
        default=Path("experiments/manifests/benchmark_subset_manifest.json"),
    )
    parser.add_argument(
        "--verify-disk",
        action="store_true",
        default=None,
        help="Require all media files even when no declared media directory is present.",
    )
    args = parser.parse_args(argv)

    holdout_groups: set[str] = set()
    holdout_ids: set[str] = set()
    if args.benchmark_manifest.is_file():
        bench = json.loads(args.benchmark_manifest.read_text(encoding="utf-8"))
        for art in bench.get("media_artifacts", []):
            if art.get("split") == "m6_holdout":
                holdout_groups.add(str(art["source_group_id"]))
                holdout_ids.add(str(art["artifact_id"]))

    videos = load_dev_seed_manifest(args.manifest)
    annotations = load_dev_seed_annotations(args.annotations)
    summary = validate_dev_seed_dataset(
        videos,
        annotations,
        repo_root=Path.cwd(),
        verify_files_on_disk=args.verify_disk,
        holdout_source_group_ids=holdout_groups,
        holdout_video_ids=holdout_ids,
    )
    print(
        f"D0 dev_seed valid: videos={summary.video_count}, "
        f"questions={summary.question_count} "
        f"(answerable={summary.answerable_count}, "
        f"not_answerable={summary.not_answerable_count}), "
        f"m1_5_slice={summary.m1_5_slice_video_ids}, "
        f"on_disk_verified={summary.verified_on_disk_files}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
