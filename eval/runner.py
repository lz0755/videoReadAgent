"""Run M6 engineering or explicitly approved blind evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Coroutine
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from eval.manifest import (
    D1_RECEIPT_SHA,
    M6_MANIFESTS,
    DatasetManifestError,
    EvalDataset,
    _compute_file_sha256,
    d1_sources,
    load_eval_dataset,
)
from eval.report import latency, save_json, trial_evidence, usage_totals
from experiments.baseline_runner import measure_process_peak_rss_bytes
from video_agent.adapters.llm.deepseek import slice_endpoint
from video_agent.adapters.storage.media_cache import model_directory_digest
from video_agent.adapters.storage.migrate import run_migrations
from video_agent.agent.tools import tool_schema
from video_agent.application.budget import SliceRunLimits
from video_agent.application.prompt_registry import PromptRegistry, canonical_json_sha256
from video_agent.bootstrap import (
    ask_registered_asset,
    build_asset_importer,
    index_registered_asset,
    load_app_config,
)
from video_agent.cli import _run_offline_coro
from video_agent.domain.config import AppConfig
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.ports import PlannerResponse, VisionResponse

type Row = dict[str, Any]
type Mode = Literal["offline", "live"]


def _approval(args: argparse.Namespace, config: AppConfig) -> SliceRunLimits:
    if args.output_dir is None or args.budget is None:
        raise DatasetManifestError("explicit_output_and_budget_required")
    approval = yaml.safe_load(args.budget.read_bytes())
    if not isinstance(approval, dict):
        raise DatasetManifestError("invalid_approval")
    auth = approval.get("authorization")
    if (
        not isinstance(auth, dict)
        or auth.get("live_enabled") is not True
        or auth.get("cloud_egress_authorized") is not True
    ):
        raise DatasetManifestError("explicit_live_consent_required")
    if (
        approval.get("purpose") != "m6_heldout"
        or not isinstance(approval.get("authorization_ref"), str)
        or not approval["authorization_ref"].strip()
        or approval.get("provider") != config.provider.name
        or approval.get("model") != config.provider.model
        or not approval.get("output_dir")
        or Path(approval["output_dir"]).resolve() != args.output_dir.resolve()
    ):
        raise DatasetManifestError("approval_scope_mismatch")
    _, videos = d1_sources()
    hashes = [v["sha256"] for v in videos if v["split"] == "m6_holdout"]
    if sorted(approval.get("authorized_media_sha256", [])) != sorted(hashes):
        raise DatasetManifestError("media_egress_scope_mismatch")
    limits = SliceRunLimits.model_validate(approval.get("budget"))
    if any(
        value is None
        for value in (
            limits.max_provider_attempts,
            limits.max_submitted_image_parts,
            limits.max_estimated_cost_usd,
            limits.deadline_seconds,
        )
    ):
        raise DatasetManifestError("aggregate_budget_incomplete")
    slice_endpoint(config.provider.name, config.provider.model, config.provider.api_key_env)
    if not os.environ.get(config.provider.api_key_env, "").strip():
        raise DatasetManifestError("credential_missing")
    if args.asr_model is None:
        raise DatasetManifestError("local_asr_weights_required")
    return limits


def _remaining(limits: SliceRunLimits, totals: Row, seconds: float) -> SliceRunLimits:
    assert limits.max_provider_attempts is not None and limits.max_submitted_image_parts is not None
    assert limits.max_estimated_cost_usd is not None
    if totals["unknown_cost_records"] or totals["pending_reservations"]:
        raise DatasetManifestError("EXTERNAL_RESULT_UNKNOWN")
    if seconds <= 0:
        raise DatasetManifestError("DEADLINE_EXCEEDED")
    attempts = limits.max_provider_attempts - totals["committed_attempts"]
    images = limits.max_submitted_image_parts - totals["committed_image_parts"]
    usd = limits.max_estimated_cost_usd - totals["committed_cost_usd"]
    if attempts <= 0 or images < 0 or usd <= 0:
        raise DatasetManifestError("BUDGET_EXCEEDED")
    return limits.model_copy(
        update={
            "max_provider_attempts": attempts,
            "max_submitted_image_parts": images,
            "max_estimated_cost_usd": usd,
            "deadline_seconds": seconds,
        }
    )


def _provenance(
    config: AppConfig, args: argparse.Namespace, dataset: EvalDataset, limits: SliceRunLimits | None
) -> Row:
    root = M6_MANIFESTS.parents[1]
    files = [
        *root.glob("src/video_agent/**/*.py"),
        *root.glob("src/video_agent/**/*.sql"),
        *root.glob("eval/*.py"),
        root / "experiments/baseline_runner.py",
    ]
    code = canonical_json_sha256(
        {p.relative_to(root).as_posix(): _compute_file_sha256(p) for p in files}
    )
    commit = None
    if (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            commit = proc.stdout.strip()
        except OSError, subprocess.SubprocessError:
            pass
    prompts = PromptRegistry(Path(config.prompts.registry_path))
    event_prompts = PromptRegistry(
        Path(config.prompts.registry_path).parent / "vision/event_registry.json"
    )
    vision_schema = VisionResponse.model_json_schema()
    prices = Path(config.billing.price_config_path)
    if args.live and config.provider.name == "openai":
        prices = prices.with_name("prices_openai.yaml")
    return {
        "git_commit": commit,
        "source_tree_sha256": code,
        "lock_sha256": _compute_file_sha256(root / "uv.lock"),
        "input_sha256": dataset.input_sha256,
        "freeze_receipt_sha256": D1_RECEIPT_SHA if dataset.split != "fixture" else None,
        "media_sha256": {vid: v.sha256 for vid, v in dataset.videos.items()},
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "pyav": version("av"),
        "config_sha256": _compute_file_sha256(args.config),
        "prices_sha256": _compute_file_sha256(prices),
        "schema_sha256": _compute_file_sha256(root / "src/video_agent/adapters/storage/schema.sql"),
        "prompt_bundles": {
            "index": prompts.compute_bundle_hash(tool_schema={}, output_schema=vision_schema),
            "planner": prompts.compute_bundle_hash(
                tool_schema=tool_schema(), output_schema=PlannerResponse.model_json_schema()
            ),
            "event": event_prompts.compute_bundle_hash(tool_schema={}, output_schema=vision_schema),
        },
        "models": {
            "vision": config.provider.model if args.live else "fake-vision",
            "planner": config.provider.model if args.live else "fake-planner",
            "asr": model_directory_digest(args.asr_model) if args.live else "fake-asr-v1",
        },
        "sampling": config.analysis.model_dump(),
        "qa_limits": config.qa.model_dump(),
        "profiles": {vid: video.profile for vid, video in dataset.videos.items()},
        "budget": limits.model_dump(exclude_none=True) if limits else None,
        "approval_sha256": _compute_file_sha256(args.budget) if limits else None,
    }


def _reason(exc: Exception) -> str:
    if isinstance(exc, VideoAgentError):
        return exc.code.value
    if isinstance(exc, DatasetManifestError):
        return str(exc)
    return "EVALUATION_ERROR"


def _run(
    dataset: EvalDataset,
    args: argparse.Namespace,
    config: AppConfig,
    out: Path,
    limits: SliceRunLimits | None,
    provenance: Row,
) -> int:
    mode: Mode = "live" if args.live else "offline"
    root = out / "managed"
    db = root / "agent.sqlite3"
    root.mkdir()
    run_migrations(db, data_root=root, writers_stopped=True)
    importer = build_asset_importer(root, config)
    started = time.monotonic()
    deadline = started + limits.deadline_seconds if limits and limits.deadline_seconds else None
    approval_file = out / "task-approval.json"
    trials: dict[str, Row] = {
        r.request_id: {"video_id": r.video_id, "mode": r.mode, "execution": "not_run"}
        for r in dataset.requests
    }
    indexes: dict[str, Row] = {}
    report: Row = {
        "schema_version": 1,
        "mode": "live" if args.live else "offline_fake",
        "quality": None,
        "state": "running",
        "split": dataset.split,
        "started_at": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        "populations": dict(Counter(r.mode for r in dataset.requests)),
        "indexes": indexes,
    }

    def task_approval() -> Path | None:
        if limits is None or deadline is None:
            return None
        left = _remaining(limits, usage_totals(db), deadline - time.monotonic())
        save_json(
            approval_file,
            {
                "authorization": {"live_enabled": True, "cloud_egress_authorized": True},
                "budget": left.model_dump(exclude_none=True),
            },
        )
        return approval_file

    def call[T](coro: Coroutine[Any, Any, T]) -> T:
        if deadline is None:
            return _run_offline_coro(coro)

        async def bounded() -> T:
            assert deadline is not None
            left = deadline - time.monotonic()
            if left <= 0:
                coro.close()
                raise DatasetManifestError("DEADLINE_EXCEEDED")
            try:
                async with asyncio.timeout(left):
                    return await coro
            except TimeoutError:
                raise DatasetManifestError("DEADLINE_EXCEEDED") from None

        try:
            return asyncio.run(bounded())
        finally:
            approval_file.unlink(missing_ok=True)

    def checkpoint() -> None:
        report["trials_sha256"] = save_json(out / "trials.json", trials)
        report["usage"] = usage_totals(db)
        report["elapsed_s"] = time.monotonic() - started
        report["peak_rss_bytes"], report["rss_method"] = measure_process_peak_rss_bytes()
        report["latency"] = latency(trials)
        report["unfinished"] = [
            rid
            for rid, row in trials.items()
            if row["execution"] != "completed"
            or row.get("failure")
            in {"BUDGET_EXCEEDED", "DEADLINE_EXCEEDED", "EXTERNAL_RESULT_UNKNOWN"}
        ]
        report["failures"] = {
            rid: row["failure"] for rid, row in trials.items() if row.get("failure")
        }
        report["answer_statuses"] = dict(
            Counter(row["answer_status"] for row in trials.values() if "answer_status" in row)
        )
        save_json(out / "report.json", report)

    checkpoint()
    for vid, video in dataset.videos.items():
        index: Row = {}
        indexes[vid] = index
        tick = time.monotonic()
        try:
            asset = importer.import_file(
                video.path, asset_id="asset_" + uuid.uuid4().hex, authorization=video.authorization
            )
            if asset.sha256 != video.sha256:
                raise DatasetManifestError("media_hash_mismatch")
            index.update(asset_id=asset.asset_id, duration_ms=asset.duration_ms)
            index.update(
                call(
                    index_registered_asset(
                        data_root=root,
                        asset_id=asset.asset_id,
                        analysis_id=None,
                        profile=video.profile,
                        mode=mode,
                        config_path=args.config,
                        budget_config_path=task_approval(),
                        asr_model_path=args.asr_model,
                    )
                )
            )
        except Exception as exc:
            index["failure"] = _reason(exc)
        index["elapsed_s"] = time.monotonic() - tick
        if index.get("duration_ms"):
            index["rtf"] = 1000 * index["elapsed_s"] / index["duration_ms"]
        for request in dataset.requests:
            if request.video_id != vid:
                continue
            trial = trials[request.request_id]
            if "failure" in index:
                trial["failure"] = index["failure"]
                continue
            tick = time.monotonic()
            try:
                trial.update(
                    execution="running",
                    asset_id=index["asset_id"],
                    analysis_id=index["analysis_id"],
                )
                checkpoint()
                scoped = task_approval()
                response = call(
                    ask_registered_asset(
                        data_root=root,
                        asset_id=index["asset_id"],
                        analysis_id=index["analysis_id"],
                        query=request.query,
                        question_mode=request.mode,
                        mode=mode,
                        config_path=args.config,
                        budget_config_path=scoped,
                    )
                )
                trial.update(question_id=response["question_id"], qa_job_id=response["qa_job_id"])
                trial.update(
                    trial_evidence(
                        root, response, asset=index["asset_id"], analysis=index["analysis_id"]
                    )
                )
            except Exception as exc:
                trial.update(execution="failed", failure=_reason(exc))
                if isinstance(exc, VideoAgentError):
                    trial.update(
                        {
                            k: exc.details[k]
                            for k in ("question_id", "qa_job_id")
                            if k in exc.details
                        }
                    )
            trial["elapsed_s"] = time.monotonic() - tick
            if "question_id" in trial:
                usage = usage_totals(db, trial["question_id"])
                trial.update(usage=usage, expensive_tool_calls=usage["expensive_tool_calls"])
            checkpoint()
        checkpoint()
    report["state"] = "finished"
    checkpoint()
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "state": report["state"],
                "populations": report["populations"],
                "unfinished": len(report["unfinished"]),
                "quality": None,
            }
        )
    )
    return int(bool(report["unfinished"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=M6_MANIFESTS / "dev.jsonl")
    parser.add_argument("--event-manifest", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--media-root", type=Path, default=Path("data/d1-evaluator/frozen_root"))
    parser.add_argument("--output-dir", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/defaults.yaml"))
    args = parser.parse_args(argv)
    try:
        config = load_app_config(args.config)
        if not args.live and (args.budget or args.asr_model):
            raise DatasetManifestError("model_or_budget_requires_live")
        limits = _approval(args, config) if args.live else None
        dataset = load_eval_dataset(
            args.manifest, args.event_manifest, args.inputs, args.media_root
        )
        if args.live and dataset.split != "heldout":
            raise DatasetManifestError("live_requires_frozen_heldout")
        provenance = _provenance(config, args, dataset, limits)
        out = args.output_dir or Path("data/m6") / ("offline-" + uuid.uuid4().hex)
        out.mkdir(parents=True, exist_ok=False)
        if args.output_dir is None:
            print(f"Output: data/m6/{out.name}")
        return _run(dataset, args, config, out, limits, provenance)
    except (ValueError, OSError, KeyError, TypeError, VideoAgentError, yaml.YAMLError) as exc:
        reason = _reason(exc)
        if isinstance(exc, FileExistsError):
            reason = "output_already_exists"
        elif isinstance(exc, FileNotFoundError):
            reason = "input_missing"
        elif isinstance(exc, ValidationError):
            reason = "invalid_configuration"
        print(json.dumps({"status": "blocked", "reason": reason}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
