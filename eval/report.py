"""M6 evidence, usage receipts and evaluator-only adjudication of a sealed run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from contextlib import closing
from pathlib import Path
from typing import Any

from eval.d1 import _receipt
from eval.manifest import (
    D1_BLIND_SHA,
    D1_RECEIPT_SHA,
    d1_sources,
    pinned_json,
    validate_split_groups,
)
from eval.metrics import score
from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.adapters.storage.filesystem import CatalogArtifactStore
from video_agent.adapters.storage.qa import resolve_evidence
from video_agent.domain.errors import ErrorCode
from video_agent.domain.models import Answer, BudgetCounters, FrameEvidence, UsageRecord
from video_agent.domain.ports import SearchHit

type Row = dict[str, Any]


def save_json(path: Path, obj: object) -> str:
    data = (
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)
    return hashlib.sha256(data).hexdigest()


def usage_totals(db: Path, scope_id: str | None = None) -> Row:
    """Read durable reservations and settlements, including operations that raised."""
    with closing(connect_sqlite(db)) as conn:
        accounts = conn.execute(
            "SELECT consumed_json,reserved_json FROM budget_accounts WHERE ? IS NULL OR scope_id=?",
            (scope_id, scope_id),
        ).fetchall()
        attempts = conn.execute(
            "SELECT usage_json, json_extract(reservation_json,'$.dispatched') "
            "FROM usage_records WHERE ? IS NULL OR scope_id=? ORDER BY rowid",
            (scope_id, scope_id),
        ).fetchall()
    counters = [BudgetCounters.model_validate_json(value) for row in accounts for value in row]
    records = [
        (UsageRecord.model_validate_json(value), dispatched) for value, dispatched in attempts
    ]
    pending = sum(r.status == "pending" for r, _ in records)
    sent = [
        r
        for r, dispatched in records
        if r.status in {"succeeded", "failed", "unknown"}
        or (r.status == "pending" and (dispatched or r.images_sent))
    ]
    unknown = sum(r.status in {"unknown", "pending"} or r.cost_status == "unknown" for r in sent)
    return {
        "provider_attempts": len(sent),
        "submitted_image_parts": sum(r.images_sent for r in sent),
        "pending_reservations": pending,
        "unknown_cost_records": unknown,
        "cost_status": "unknown"
        if unknown or pending
        else "estimated"
        if any(r.cost_status == "estimated" for r in sent)
        else "confirmed",
        "confirmed_cost_usd": sum(r.confirmed_cost_usd or 0.0 for r in sent),
        "estimated_cost_usd": sum(r.estimated_cost_usd for r in sent),
        "unknown_cost_usd": None if unknown or pending else 0,
        "reported_input_tokens": sum(r.input_tokens or 0 for r in sent),
        "reported_output_tokens": sum(r.output_tokens or 0 for r in sent),
        "missing_token_records": sum(
            r.input_tokens is None or r.output_tokens is None for r in sent
        ),
        "tool_calls": sum(c.tool_calls for c in counters),
        "expensive_tool_calls": sum(c.expensive_tool_calls for c in counters),
        "committed_attempts": sum(c.provider_attempts for c in counters),
        "committed_image_parts": sum(c.submitted_image_parts for c in counters),
        "committed_cost_usd": sum(
            max(c.estimated_cost_usd, c.confirmed_cost_usd) for c in counters
        ),
        "provider_models": sorted({(r.provider + "/" + r.model_id) for r in sent}),
    }


def trial_evidence(root: Path, result: Row, *, asset: str, analysis: str) -> Row:
    db = root / "agent.sqlite3"
    files = CatalogArtifactStore(root)
    answer = Answer.model_validate(result["answer"])
    job, question = result["qa_job_id"], result["question_id"]
    if (answer.asset_id, answer.analysis_id, answer.question_id, answer.trace_id) != (
        asset,
        analysis,
        question,
        job,
    ):
        raise ValueError("answer_scope_mismatch")
    cache: dict[str, Row | None] = {}

    def witness(eid: str) -> Row | None:
        if eid not in cache:
            item = resolve_evidence(db, files, asset, analysis, eid)
            if isinstance(item, FrameEvidence):
                cache[eid] = {"kind": "frame", "point_ms": item.actual_pts_ms}
            elif item is not None:
                cache[eid] = {"kind": "transcript", **item.time_range.model_dump()}
            else:
                cache[eid] = None
        return cache[eid]

    checks = [witness(eid) is not None for claim in answer.claims for eid in claim.evidence_ids]
    candidates = []
    for value in result["first_hits"][:5]:
        hit = SearchHit.model_validate(value)
        group = []
        if (hit.asset_id, hit.analysis_id) == (asset, analysis):
            group = [w for eid in hit.evidence_ids if (w := witness(eid)) is not None]
        candidates.append(group)
    predictions = []
    loc = answer.localization
    if loc is not None:
        bound = []
        for side in (loc.start_bracket, loc.end_bracket):
            for eid, point in (
                (side.before_evidence_id, side.lower_ms),
                (side.after_evidence_id, side.upper_ms),
            ):
                bound.append(witness(eid) == {"kind": "frame", "point_ms": point})
        checks.extend(bound)
        if loc.refined and all(bound):
            predictions.append(
                [loc.label, loc.start_bracket.estimate_ms, loc.end_bracket.estimate_ms]
            )
    failures = {code.value for code in ErrorCode}
    failure = next(
        (gap.split(":", 1)[0] for gap in answer.gaps if gap.split(":", 1)[0] in failures), None
    )
    return {
        "execution": "completed",
        "asset_id": asset,
        "analysis_id": analysis,
        "question_id": question,
        "qa_job_id": job,
        "answer_status": answer.status,
        "failure": failure,
        "claim_count": len(answer.claims),
        "citation_checks": checks,
        "first_candidates": candidates,
        "predictions": predictions,
        "first_hits": result["first_hits"],
        "answer": answer.model_dump(),
        "expensive_tool_calls": result["consumed"]["expensive_tool_calls"],
    }


def latency(trials: dict[str, Row]) -> Row:
    groups: dict[str, list[Row]] = {
        "qa_without_review": [],
        "qa_with_review": [],
        "event_localization": [],
    }
    for t in trials.values():
        if "elapsed_s" in t:
            key = (
                "event_localization"
                if t["mode"] == "event_localization"
                else ("qa_with_review" if t.get("expensive_tool_calls") else "qa_without_review")
            )
            groups[key].append(t)
    result: Row = {}
    for key, values in groups.items():
        ordered = sorted(t["elapsed_s"] for t in values)
        accounts = [t["usage"] for t in values if t.get("usage") is not None]
        usage = None
        if accounts:
            fields = (
                "provider_attempts",
                "submitted_image_parts",
                "reported_input_tokens",
                "reported_output_tokens",
                "missing_token_records",
                "confirmed_cost_usd",
                "estimated_cost_usd",
                "pending_reservations",
                "unknown_cost_records",
                "tool_calls",
                "expensive_tool_calls",
            )
            usage = {field: sum(account[field] for account in accounts) for field in fields}
            usage["unknown_cost_usd"] = (
                None if usage["pending_reservations"] or usage["unknown_cost_records"] else 0
            )
        result[key] = {
            "n": len(values),
            "usage": usage,
            "jobs_without_usage": len(values) - len(accounts),
            **{
                f"p{p}_s": ordered[math.ceil(p * len(ordered) / 100) - 1] if ordered else None
                for p in (50, 95)
            },
        }
    return result


def adjudicate(directory: Path, packet_path: Path, reviews_path: Path) -> Row:
    run = json.loads((directory / "report.json").read_bytes())
    provenance = run["provenance"]
    if (
        run["mode"] != "live"
        or run["split"] != "heldout"
        or run["state"] != "finished"
        or provenance["input_sha256"] != D1_BLIND_SHA
        or provenance.get("freeze_receipt_sha256") != D1_RECEIPT_SHA
        or not provenance.get("approval_sha256")
        or not provenance.get("budget")
        or any(str(model).startswith("fake-") for model in provenance["models"].values())
    ):
        raise ValueError("formal_blind_run_required")
    trials = pinned_json(directory / "trials.json", run["trials_sha256"])
    root = directory / "managed"
    db = root / "agent.sqlite3"
    if (
        not db.is_file()
        or usage_totals(db) != run["usage"]
        or any(model.startswith("fake/") for model in run["usage"]["provider_models"])
    ):
        raise ValueError("live_ledger_mismatch")
    # Reopen every completed answer and resolve its original citations on disk.
    # Missing/corrupt evidence must still count in the integrity denominator.
    with closing(connect_sqlite(db)) as conn:
        for trial in trials.values():
            if trial["execution"] != "completed":
                continue
            row = conn.execute(
                "SELECT a.payload FROM answers a JOIN jobs j "
                "USING(question_id,asset_id,analysis_id) "
                "WHERE j.job_id=? AND a.asset_id=? AND a.analysis_id=? AND a.question_id=?",
                (trial["qa_job_id"], trial["asset_id"], trial["analysis_id"], trial["question_id"]),
            ).fetchone()
            if row is None or json.loads(row[0]) != trial["answer"]:
                raise ValueError("answer_receipt_mismatch")
            result = trial | {"consumed": {"expensive_tool_calls": trial["expensive_tool_calls"]}}
            trial.update(
                trial_evidence(root, result, asset=trial["asset_id"], analysis=trial["analysis_id"])
            )
    reviews = json.loads(reviews_path.read_bytes())
    if reviews["trials_sha256"] != run["trials_sha256"]:
        raise ValueError("reviews_belong_to_another_run")
    receipt, sources = d1_sources()
    packet = pinned_json(packet_path, receipt["packet_sha256"])
    validate_split_groups([v | packet["groups"][v["artifact_id"]] for v in sources])
    held = {v["artifact_id"]: v for v in sources if v["split"] == "m6_holdout"}
    questions = [q for q in packet["questions"] if q["video_id"] in held]
    by_event = {e["event_id"]: e for e in packet["events"]}
    events = [by_event[r["event_id"]] | {"request_id": r["request_id"]} for r in packet["requests"]]
    if trials.keys() != ({q["question_id"] for q in questions} | {e["request_id"] for e in events}):
        raise ValueError("incomplete_trial_register")
    for ref in [*questions, *events]:
        source = held[ref["video_id"]]
        ref["benchmark"] = source["benchmark"]
        if source["benchmark"] == "videogui":
            ref["scene"] = "software"
        elif "Action & Motion" in source["task_categories"]:
            ref["scene"] = "action"
        else:
            ref["scene"] = "other_or_unlabelled"
    result = score(questions, events, trials, reviews["reviews"])
    return {
        **result,
        "packet_sha256": receipt["packet_sha256"],
        "trials_sha256": run["trials_sha256"],
        "reviews_sha256": hashlib.sha256(reviews_path.read_bytes()).hexdigest(),
        "population": {"qa": 32, "event_localization": 20},
        "asr_cer_wer": {"value": None, "reason": "no_frozen_verbatim_speech_reference"},
        "scene_basis": "D1 benchmark and task categories. Lecture has no separate frozen label.",
        "provenance": run["provenance"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = adjudicate(args.run_dir, args.packet, args.reviews)
        panel = report["second_reviews"]
        reviewed = panel["independent"] + panel["delayed_self"]
        filename = "quality.json"
        if report["pending_reviews"] or 5 * reviewed < panel["population"]:
            filename = f"quality-review-{report['reviews_sha256']}.json"
        content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        _receipt(args.run_dir / filename, (content + "\n").encode())
        print(
            json.dumps({"quality_passed": report["quality_passed"], "metrics": report["metrics"]})
        )
        return 0 if report["quality_passed"] else 1
    except OSError, ValueError, KeyError, TypeError:
        print('{"status":"blocked","reason":"invalid_or_previously_scored_run"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
