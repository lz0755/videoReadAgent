"""Evaluator-local D1 audit. The public receipt contains only counts and hashes."""

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from typing import Any

import av
from PIL import Image

from eval.manifest import (
    _compute_file_sha256,
    _validate_evidence_refs,
    load_dev_seed_annotations,
    load_dev_seed_manifest,
    validate_dev_seed_dataset,
)
from experiments.dataset_curator import validate_subset_manifest
from video_agent.domain.models import compute_canonical_pts_ms

_SOURCE = Path("experiments/manifests/benchmark_subset_manifest.json")
_SEED = Path("eval/manifests/dev_seed.jsonl")
_SEED_QA = Path("eval/annotations/dev_seed/dev_seed_questions.json")
_SPLITS = {"m0_dev": 6, "d1_dev_extra": 10, "m6_holdout": 8}
Row = dict[str, Any]


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty text")
    return value.strip()


def _integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("expected strict integer")
    return value


def _rows(value: Any, size: int) -> list[Row]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or not all(isinstance(row, dict) for row in value)
    ):
        raise ValueError("invalid record count or shape")
    return value


def _local(root: Path, name: Any) -> Path:
    rel = _text(name)
    portable = PureWindowsPath(rel)
    _check(not portable.anchor and ".." not in portable.parts, "expected relative evidence path")
    path = (root / rel).resolve(strict=True)
    _check(path.is_relative_to(root.resolve()) and path.is_file(), "evidence outside root")
    return path


def _media(videos: list[Row], root: Path) -> dict[str, Row]:
    _check(Counter(v["split"] for v in videos) == _SPLITS, "incorrect video split")
    indexed = {}
    seen: set[str] = set()
    for v in videos:
        vid = _text(v["artifact_id"])
        path = _local(root, v["relative_path"])
        sha = _text(v["sha256"])
        _check(
            v["media_kind"] == "video" and vid not in indexed and sha not in seen,
            "duplicate or invalid video",
        )
        _check(_integer(v["ffprobe"]["duration_ms"]) > 0, "invalid source duration")
        if "byte_size" in v:
            _check(path.stat().st_size == _integer(v["byte_size"]), "source size mismatch")
        _check(_compute_file_sha256(path) == sha, "source hash mismatch")
        indexed[vid] = v
        seen.add(sha)
    return indexed


def _groups(indexed: dict[str, Row], groups: Any) -> None:
    _check(isinstance(groups, dict) and groups.keys() == indexed.keys(), "incomplete split groups")
    sides: dict[tuple[str, str], bool] = {}
    for vid, v in indexed.items():
        holdout = v["split"] == "m6_holdout"
        for key, value in (
            ("source", v["source_group_id"]),
            ("original", v["original_id"]),
            ("series", groups[vid]["series"]),
            ("related", groups[vid]["related"]),
        ):
            identity = (key, _text(value).casefold())
            _check(sides.setdefault(identity, holdout) == holdout, "shared source across splits")


def _questions(
    indexed: dict[str, Row], seed: list[Row], extra: Any
) -> tuple[set[str], set[tuple[str, str]]]:
    ids: set[str] = set()
    texts: set[tuple[str, str]] = set()
    per_video: Counter[str] = Counter()
    held: Counter[str] = Counter()
    for source, is_seed in ((_rows(seed, 24), True), (_rows(extra, 72), False)):
        for q in source:
            vid, qid = _text(q["video_id"]), _text(q["question_id"])
            question = (vid, _text(q["question_text"]).casefold())
            _check(
                vid in indexed and (indexed[vid]["split"] == "m0_dev") == is_seed,
                "question outside its split",
            )
            _check(qid not in ids and question not in texts, "duplicate question")
            ids.add(qid)
            texts.add(question)
            per_video[vid] += 1
            answerability = _text(q["answerability"])
            _text(q["category"])
            _text(q["verification_basis"])
            _validate_evidence_refs(
                q["evidence_refs"],
                question_id=qid,
                answerability=answerability,
                duration_ms=indexed[vid]["ffprobe"]["duration_ms"],
            )
            if answerability == "answerable":
                _text(q["reference_answer"])
            else:
                gaps = q["expected_gaps"]
                _check(
                    answerability == "not_answerable"
                    and q["reference_answer"] is None
                    and isinstance(gaps, list)
                    and bool(gaps)
                    and all(_text(g) for g in gaps),
                    "invalid abstention",
                )
            if indexed[vid]["split"] == "m6_holdout":
                held[answerability] += 1
    _check(
        per_video == dict.fromkeys(indexed, 4) and held == {"answerable": 24, "not_answerable": 8},
        "incorrect QA denominators",
    )
    return ids, texts


def _rational(value: Any) -> Fraction:
    _check(isinstance(value, list) and len(value) == 2, "invalid time base")
    numerator, denominator = map(_integer, value)
    _check(numerator > 0 and denominator > 0, "invalid time base")
    return Fraction(numerator, denominator)


def _verify_frames(video: Row, proofs: list[Row], *, repo_root: Path, evidence_root: Path) -> None:
    required: dict[tuple[int, Fraction], list[Row]] = defaultdict(list)
    for ref in proofs:
        _check(ref["source_sha256"] == video["sha256"], "proof belongs to another source")
        required[(_integer(ref["raw_pts"]), _rational(ref["time_base"]))].append(ref)

    with av.open(str(_local(repo_root, video["relative_path"]))) as container:
        origin: tuple[int, Fraction] | None = None
        for frame in container.decode(video=0):
            if frame.pts is None or frame.time_base is None:
                continue
            stamp = (frame.pts, Fraction(frame.time_base.numerator, frame.time_base.denominator))
            if origin is None:
                origin = stamp
            matching = required.pop(stamp, None)
            if matching is None:
                continue
            pts, base = stamp
            origin_pts, origin_base = origin
            ms = compute_canonical_pts_ms(
                raw_pts=pts,
                time_base_num=base.numerator,
                time_base_den=base.denominator,
                origin_pts=origin_pts,
                origin_time_base_num=origin_base.numerator,
                origin_time_base_den=origin_base.denominator,
                duration_ms=video["ffprobe"]["duration_ms"],
            )
            decoded = frame.to_image()  # type: ignore[no-untyped-call]
            pixels = decoded.tobytes()
            checked: set[tuple[Path, str]] = set()
            for ref in matching:
                _check(
                    _integer(ref["origin_pts"]) == origin_pts
                    and _rational(ref["origin_time_base"]) == origin_base
                    and _integer(ref["actual_pts_ms"]) == ms,
                    "incorrect presentation timeline",
                )
                path = _local(evidence_root, ref["path"])
                checksum = _text(ref["sha256"])
                if (path, checksum) in checked:
                    continue
                _check(_compute_file_sha256(path) == checksum, "frame checksum mismatch")
                with Image.open(path) as saved:
                    _check(
                        saved.format == "PNG"
                        and saved.size == decoded.size
                        and saved.convert("RGB").tobytes() == pixels,
                        "frame pixels differ from source",
                    )
                checked.add((path, checksum))
            if not required:
                break
    _check(not required, "specified raw presentation timestamp missing")


def _packet(
    indexed: dict[str, Row], seed: list[Row], packet: Row, evidence_root: Path, repo_root: Path
) -> None:
    _groups(indexed, packet["groups"])
    review = packet["review"]
    _text(review["coverage_note"])
    if any(v["ffprobe"]["duration_ms"] < 30000 for v in indexed.values()):
        _text(review["short_clip_decision"])
    ids, questions = _questions(indexed, seed, packet["questions"])
    events: dict[str, str] = {}
    proofs: dict[str, list[Row]] = defaultdict(list)
    held = {vid for vid, v in indexed.items() if v["split"] == "m6_holdout"}
    for event in _rows(packet["events"], 20):
        eid, vid = _text(event["event_id"]), _text(event["video_id"])
        _text(event["label"])
        _check(eid not in ids and vid in held, "invalid event identity")
        ids.add(eid)
        events[eid] = vid
        left, right = _integer(event["start_ms"]), _integer(event["end_ms"])
        _check(
            0 <= left < right <= indexed[vid]["ffprobe"]["duration_ms"], "invalid event interval"
        )
        for field, point in (("start_frames", left), ("end_frames", right)):
            before, after = _rows(event[field], 2)
            a, b = _integer(before["actual_pts_ms"]), _integer(after["actual_pts_ms"])
            _check(a < b and a <= point <= b, "boundary outside presentation frames")
            proofs[vid].extend([before, after])
    _check(set(proofs) == held, "events missing for a held-out video")
    requested: set[str] = set()
    for request in _rows(packet["requests"], 20):
        eid, rid, vid = (
            _text(request["event_id"]),
            _text(request["request_id"]),
            _text(request["video_id"]),
        )
        query = (vid, _text(request["query_text"]).casefold())
        _check(
            rid not in ids
            and query not in questions
            and eid not in requested
            and events.get(eid) == vid
            and request["mode"] == "event_localization",
            "invalid localization request",
        )
        ids.add(rid)
        questions.add(query)
        requested.add(eid)
    for vid, refs in proofs.items():
        _verify_frames(indexed[vid], refs, evidence_root=evidence_root, repo_root=repo_root)


def verify_packet(
    videos: list[Row], seed: list[Row], packet: Row, *, evidence_root: Path, repo_root: Path
) -> None:
    """Verify split, questions, four actual decoded proof frames per event and request binding."""
    try:
        _packet(_media(videos, repo_root), seed, packet, evidence_root, repo_root)
    except (KeyError, TypeError, IndexError, OSError) as exc:
        raise ValueError("invalid packet or inaccessible evidence") from exc


def audit(*, repo_root: Path, packet_path: Path | None = None) -> Row:
    report: Row = {"status": "blocked", "reason": "source_or_dev_seed_invalid"}
    try:
        manifest = json.loads((repo_root / _SOURCE).read_bytes())
        validate_subset_manifest(manifest)
        videos = [v for v in manifest["media_artifacts"] if v["media_kind"] == "video"]
        indexed = _media(videos, repo_root)
        seed_media = load_dev_seed_manifest(repo_root / _SEED)
        annotations = load_dev_seed_annotations(repo_root / _SEED_QA)
        held = {key: v for key, v in indexed.items() if v["split"] == "m6_holdout"}
        validated = validate_dev_seed_dataset(
            seed_media,
            annotations,
            repo_root=repo_root,
            verify_files_on_disk=True,
            holdout_source_group_ids={v["source_group_id"] for v in held.values()},
            holdout_video_ids=set(held),
        )
        for record in seed_media:
            source = indexed[record["video_id"]]
            _check(
                source["split"] == "m0_dev"
                and record["sha256"] == source["sha256"]
                and record["duration_ms"] == source["ffprobe"]["duration_ms"],
                "D0 source mismatch",
            )
        report.update(
            {
                "source_manifest_sha256": _compute_file_sha256(repo_root / _SOURCE),
                "dev_manifest_sha256": _compute_file_sha256(repo_root / _SEED),
                "dev_seed_qa_sha256": _compute_file_sha256(repo_root / _SEED_QA),
                "verified_videos": len(indexed),
                "dev_videos": 16,
                "heldout_videos": 8,
                "dev_seed_questions": validated.question_count,
                "clips_under_30_seconds": sum(v["ffprobe"]["duration_ms"] < 30000 for v in videos),
                "metrics_sha256": _compute_file_sha256(Path(__file__).with_name("d1_metrics.py")),
                "verifier_sha256": _compute_file_sha256(Path(__file__)),
            }
        )
        report["reason"] = "private_packet_missing"
        if packet_path is None or not packet_path.is_file():
            report["outstanding"] = {
                "dev_questions": 40,
                "heldout_questions": 32,
                "heldout_events": 20,
                "localization_requests": 20,
            }
            return report
        report["reason"] = "private_packet_invalid"
        raw = packet_path.read_bytes()
        _packet(indexed, annotations["questions"], json.loads(raw), packet_path.parent, repo_root)
        report.update(
            status="verified",
            packet_sha256=hashlib.sha256(raw).hexdigest(),
            dev_questions=64,
            heldout_answerable=24,
            heldout_unanswerable=8,
            heldout_events=20,
            localization_requests=20,
        )
        del report["reason"]
    except OSError, ValueError, TypeError, KeyError, IndexError:
        pass  # Validation errors may include private questions, labels or evaluator paths.
    return report


def _receipt(target: Path, content: bytes) -> None:
    # Publish a fully written file without replacing any previously frozen receipt.
    with NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        path.write_bytes(content)
        try:
            os.link(path, target)
        except FileExistsError:
            _check(target.read_bytes() == content, "different receipt already exists")
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    report = audit(repo_root=args.repo_root, packet_path=args.packet)
    if report["status"] == "verified" and args.receipt is not None:
        try:
            _receipt(args.receipt, (json.dumps(report, sort_keys=True) + "\n").encode())
        except OSError, ValueError:
            report = {"status": "blocked", "reason": "receipt_could_not_be_published"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
