"""CLI entrypoint for video-agent (M1.1 offline doctor + M1.5 vertical slice)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Coroutine, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import ValidationError

from video_agent.application.slice import SliceQuestionSpec, VerticalSliceResult, run_vertical_slice
from video_agent.bootstrap import (
    ask_registered_asset,
    build_asset_importer,
    build_slice_container,
    index_registered_asset,
    load_app_config,
)
from video_agent.domain.config import AppConfig
from video_agent.domain.errors import VideoAgentError


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """Single diagnostic check emitted by `video-agent doctor`."""

    name: str
    status: str  # "ok" | "notice" | "fail"
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Structured result of offline environment diagnostics."""

    ok: bool
    mode: str
    checks: tuple[DoctorCheck, ...]


def _check_binary(name: str) -> DoctorCheck:
    binary = shutil.which(name)
    if binary is None:
        return DoctorCheck(name=name, status="fail", detail=f"{name} executable not found on PATH")
    try:
        proc = subprocess.run(
            [binary, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            name=name, status="fail", detail=f"{name} probe failed: {type(exc).__name__}"
        )
    if proc.returncode != 0:
        return DoctorCheck(
            name=name, status="fail", detail=f"{name} exited with code {proc.returncode}"
        )
    first_line = proc.stdout.splitlines()[0].strip() if proc.stdout else "version unavailable"
    return DoctorCheck(name=name, status="ok", detail=first_line)


def _check_sqlite_fts5() -> DoctorCheck:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE _doctor_fts USING fts5(content)")
        return DoctorCheck(
            name="sqlite_fts5",
            status="ok",
            detail=f"sqlite {sqlite3.sqlite_version} with FTS5 enabled",
        )
    except sqlite3.Error as exc:
        return DoctorCheck(name="sqlite_fts5", status="fail", detail=f"FTS5 unavailable: {exc}")
    finally:
        conn.close()


def _check_config(config_path: Path) -> tuple[DoctorCheck, str]:
    api_key_env = "DEEPSEEK_API_KEY"
    if not config_path.is_file():
        return (
            DoctorCheck(name="config", status="fail", detail="default config file missing"),
            api_key_env,
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return (
            DoctorCheck(name="config", status="fail", detail=f"invalid YAML: {type(exc).__name__}"),
            api_key_env,
        )
    if not isinstance(raw, dict):
        return (
            DoctorCheck(name="config", status="fail", detail="config root must be a mapping"),
            api_key_env,
        )
    try:
        app_config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        return (
            DoctorCheck(
                name="config",
                status="fail",
                detail=f"AppConfig validation failed ({exc.error_count()} error(s))",
            ),
            api_key_env,
        )
    return (
        DoctorCheck(name="config", status="ok", detail="config YAML validated by AppConfig"),
        app_config.provider.api_key_env,
    )


def _check_data_root(data_root: Path) -> DoctorCheck:
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(data_root)
        free_gib = usage.free / (1024**3)
        return DoctorCheck(
            name="data_root",
            status="ok",
            detail=f"writable directory, free_gib={free_gib:.2f}",
        )
    except OSError as exc:
        return DoctorCheck(
            name="data_root",
            status="fail",
            detail=f"data_root unavailable: {type(exc).__name__}",
        )


def _check_cloud_credentials(api_key_env: str) -> DoctorCheck:
    if os.environ.get(api_key_env, "").strip():
        return DoctorCheck(
            name="cloud_credentials",
            status="ok",
            detail=f"{api_key_env} is set (value redacted); cloud egress still requires consent",
        )
    return DoctorCheck(
        name="cloud_credentials",
        status="notice",
        detail=f"{api_key_env} is not set; offline mode & fakes remain available",
    )


def run_doctor(
    *,
    config_path: Path | None = None,
    data_root: Path | None = None,
) -> DoctorReport:
    """Run offline environment diagnostics without requiring API keys or network."""
    resolved_config = config_path if config_path is not None else Path("config/defaults.yaml")
    resolved_data_root = data_root if data_root is not None else Path("data")

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_check = DoctorCheck(
        name="python",
        status="ok",
        detail=f"CPython {py_ver}",
    )
    ffmpeg_check = _check_binary("ffmpeg")
    ffprobe_check = _check_binary("ffprobe")
    sqlite_check = _check_sqlite_fts5()
    config_check, api_key_env = _check_config(resolved_config)
    data_root_check = _check_data_root(resolved_data_root)
    cred_check = _check_cloud_credentials(api_key_env)

    checks = (
        python_check,
        ffmpeg_check,
        ffprobe_check,
        sqlite_check,
        config_check,
        data_root_check,
        cred_check,
    )
    overall_ok = all(check.status != "fail" for check in checks)
    return DoctorReport(ok=overall_ok, mode="offline", checks=checks)


def _run_offline_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive pure offline coroutines without creating Windows loopback sockets."""
    try:
        coro.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    else:
        coro.close()
        raise RuntimeError("offline slice coroutine suspended unexpectedly")


def _serialize_slice_result(
    result: VerticalSliceResult, questions: Sequence[SliceQuestionSpec]
) -> dict[str, Any]:
    completed = {qr.question.question_id: qr for qr in result.question_results}
    rows = []
    for question in questions:
        qr = completed.get(question.question_id)
        if qr is not None:
            answer = qr.answer.model_dump()
            hits = qr.search_hits
            ledger = qr.qa_usage_ledger
            error_code = None
        else:
            failure = result.question_errors[question.question_id]
            answer = None
            hits = failure.search_hits
            ledger = failure.qa_usage_ledger
            error_code = failure.error_code
        rows.append(
            {
                "question_id": question.question_id,
                "query_text": question.query_text,
                "execution_status": "failed" if error_code else "completed",
                "error_code": error_code,
                "search_hit_count": len(hits),
                "answer": answer,
                "qa_usage": {
                    "total_attempts": ledger.total_attempts,
                    "total_images_sent": ledger.total_images_sent,
                    "cost_status": ledger.cost_status,
                    "estimated_cost_usd": ledger.estimated_cost_usd,
                    "confirmed_cost_usd": ledger.confirmed_cost_usd,
                },
            }
        )
    return {
        "schema_version": "1.1",
        "provider_mode": result.provider_mode,
        "asset_id": result.asset.asset_id,
        "display_filename": result.asset.display_filename,
        "duration_ms": result.asset.duration_ms,
        "analysis_id": result.analysis_id,
        "prompt_bundle_hash": result.prompt_bundle_hash,
        "audio_status": result.audio_status,
        "latency_ms": result.latency_ms,
        "frames": [
            {
                "evidence_id": f.evidence_id,
                "artifact_id": f.artifact_id,
                "requested_ms": f.requested_ms,
                "actual_pts_ms": f.actual_pts_ms,
            }
            for f in result.frames
        ],
        "transcript_segment_count": len(result.transcript_segments),
        "observation_count": len(result.observations),
        "analysis_usage": {
            "total_attempts": result.analysis_usage_ledger.total_attempts,
            "total_images_sent": result.analysis_usage_ledger.total_images_sent,
            "cost_status": result.analysis_usage_ledger.cost_status,
            "confirmed_cost_usd": result.analysis_usage_ledger.confirmed_cost_usd,
            "estimated_cost_usd": result.analysis_usage_ledger.estimated_cost_usd,
        },
        "questions": rows,
        "warnings": list(result.warnings),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-agent",
        description="Local authorized video understanding agent CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Initialize or migrate after stopping all writers")
    init.add_argument("--writers-stopped", action="store_true", required=True)
    serve = subparsers.add_parser("serve", help="Start the authenticated loopback API")
    serve.add_argument("--port", type=int, default=8000)
    worker = subparsers.add_parser("worker", help="Consume the local queue under the OS lock")
    worker.add_argument("--once", action="store_true")
    policy = worker.add_mutually_exclusive_group(required=True)
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--live", action="store_true")
    worker.add_argument("--budget-config", type=Path)
    worker.add_argument("--asr-model-path", type=Path)
    analyses = subparsers.add_parser("analyses").add_subparsers(required=True).add_parser("create")
    analyses.add_argument("--asset-id", required=True)
    analyses.add_argument("--profile", choices=("software_tutorial", "overview"), required=True)
    analyses.add_argument("--execution-mode", choices=("offline", "live"), default="offline")
    analyses.add_argument("--idempotency-key")
    questions = (
        subparsers.add_parser("questions").add_subparsers(required=True).add_parser("create")
    )
    questions.add_argument("--analysis-id", required=True)
    questions.add_argument("--text", required=True)
    questions.add_argument("--mode", choices=("qa", "event_localization"), default="qa")
    questions.add_argument("--idempotency-key")
    for entry in (init, serve, worker, analyses, questions):
        entry.add_argument("--data-root", type=Path, default=Path("data"))
        entry.add_argument("--config", type=Path, default=Path("config/defaults.yaml"))

    index = subparsers.add_parser(
        "index", help="Build or reopen a registered asset's evidence index"
    )
    index.add_argument("--asset-id", required=True)
    index.add_argument("--analysis-id", help="Resume an existing analysis with its original budget")
    index.add_argument("--profile", choices=("software_tutorial", "overview"), required=True)
    index.add_argument("--data-root", type=Path, default=Path("data"))
    index.add_argument("--config", type=Path, default=Path("config/defaults.yaml"))
    mode = index.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--offline", action="store_true", help="Use deterministic local Fake providers"
    )
    mode.add_argument(
        "--live", action="store_true", help="Use the explicitly approved cloud provider"
    )
    index.add_argument("--provider", choices=("openai", "deepseek"))
    index.add_argument("--budget-config", type=Path)
    index.add_argument("--asr-model-path", type=Path)
    index.add_argument(
        "--retry", action="store_true", help="Allow a new paid attempt for an unfinished operation"
    )
    index.add_argument("--json", action="store_true")

    ask = subparsers.add_parser("ask", help="Ask a question against a published analysis")
    ask.add_argument("--asset-id", required=True)
    ask.add_argument("--analysis-id", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--mode", choices=("qa", "event_localization"), default="qa")
    ask.add_argument("--data-root", type=Path, default=Path("data"))
    ask.add_argument("--config", type=Path, default=Path("config/defaults.yaml"))
    ask_mode = ask.add_mutually_exclusive_group(required=True)
    ask_mode.add_argument(
        "--offline", action="store_true", help="Use deterministic local Fake providers"
    )
    ask_mode.add_argument(
        "--live", action="store_true", help="Use the explicitly approved cloud provider"
    )
    ask.add_argument("--provider", choices=("openai", "deepseek"))
    ask.add_argument("--budget-config", type=Path)
    ask.add_argument("--json", action="store_true")

    assets = subparsers.add_parser("assets", help="Manage user-selected local video files")
    imports = assets.add_subparsers(dest="asset_command", required=True).add_parser(
        "import", help="Register an authorized file and retain a managed copy"
    )
    imports.add_argument("path", type=Path)
    imports.add_argument("--data-root", type=Path, default=Path("data"))
    imports.add_argument("--config", type=Path, default=Path("config/defaults.yaml"))
    imports.add_argument(
        "--authorization",
        default="local CLI user selection",
        help="Local authorization record or reference for this source",
    )
    imports.add_argument("--quota-bytes", type=int, help="Limit combined managed artifact bytes")
    imports.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check local offline runtime prerequisites without making network calls",
    )
    doctor_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/defaults.yaml"),
        help="Path to defaults YAML configuration",
    )
    doctor_parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Path to local managed data root directory",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON diagnostics",
    )

    slice_parser = subparsers.add_parser(
        "slice",
        help="Run the M1.5 real-media CLI vertical slice on a single local authorized video",
    )
    slice_parser.add_argument(
        "--video",
        type=Path,
        required=True,
        help="Path to user-selected authorized local video file",
    )
    slice_parser.add_argument(
        "--question",
        action="append",
        required=True,
        help="Question text to ask (repeatable)",
    )
    slice_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/defaults.yaml"),
        help="Path to defaults YAML configuration",
    )
    slice_parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/slice_work"),
        help="Path to local managed slice data directory",
    )
    mode_group = slice_parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--offline",
        action="store_true",
        help="Run offline pipeline verification using Fake LLM/ASR ports",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Run live cloud vision/planner and local ASR (requires consent and credentials)",
    )
    slice_parser.add_argument(
        "--budget-config",
        type=Path,
        help="Slice run approval YAML with numeric limits (required for live, optional offline).",
    )
    slice_parser.add_argument("--provider", choices=["deepseek", "openai"], help="Live provider")
    slice_parser.add_argument(
        "--cloud-egress-authorized",
        action="store_true",
        help="Explicit user authorization to transmit selected frames/text to cloud provider",
    )
    slice_parser.add_argument(
        "--asr-model-path",
        type=Path,
        default=None,
        help="Pre-downloaded local faster-whisper model directory (used in --live mode)",
    )
    slice_parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=5000,
        help="Overview frame sampling interval in milliseconds",
    )
    slice_parser.add_argument(
        "--max-frames",
        type=int,
        default=16,
        help="Maximum frames sampled for the vertical slice",
    )
    slice_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )
    return parser


def _local_command(args: argparse.Namespace) -> int:
    from video_agent.adapters.storage.migrate import run_migrations
    from video_agent.application.jobs import AnalysisRequest, QuestionRequest
    from video_agent.bootstrap import build_jobs, execute_queued_job, worker_deadlines
    from video_agent.domain.models import Job
    from video_agent.worker import run_queue

    if args.command == "init":
        versions = run_migrations(
            args.data_root / "agent.sqlite3",
            data_root=args.data_root,
            writers_stopped=args.writers_stopped,
        )
        print(json.dumps({"applied": versions}))
        return 0
    if args.command == "serve":
        import uvicorn

        from video_agent.api.app import create_app

        app = create_app(
            args.data_root, token=os.environ.get("VIDEO_AGENT_TOKEN", ""), config_path=args.config
        )
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
        return 0
    config = load_app_config(args.config)
    jobs = build_jobs(args.data_root, config)
    if args.command == "worker":
        if args.live and args.budget_config is None:
            raise ValueError("live worker requires a budget approval")

        def execute(job: Job) -> None:
            work = execute_queued_job(
                jobs,
                job,
                mode="live" if args.live else "offline",
                config_path=args.config,
                budget_config_path=args.budget_config,
                asr_model_path=args.asr_model_path,
            )
            if args.live:
                asyncio.run(work)
            else:
                _run_offline_coro(work)

        analysis, qa = worker_deadlines(config, args.budget_config)
        run_queue(
            args.data_root, jobs, execute, analysis_seconds=analysis, qa_seconds=qa, once=args.once
        )
        return 0
    if args.command == "analyses":
        result = jobs.submit_analysis(
            args.asset_id,
            AnalysisRequest(profile=args.profile, execution_mode=args.execution_mode),
            args.idempotency_key,
        )
    else:
        result = jobs.submit_question(
            args.analysis_id,
            QuestionRequest(query_text=args.text, mode=args.mode),
            args.idempotency_key,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command in {"init", "serve", "worker", "analyses", "questions"}:
        try:
            return _local_command(args)
        except (VideoAgentError, OSError, ValueError, LookupError, sqlite3.Error) as exc:
            code = exc.code.value if isinstance(exc, VideoAgentError) else "LOCAL_SERVICE_ERROR"
            print(json.dumps({"error": {"code": code}}), file=sys.stderr)
            return 1

    if args.command == "ask":
        try:
            qa = ask_registered_asset(
                data_root=args.data_root,
                asset_id=args.asset_id,
                analysis_id=args.analysis_id,
                query=args.question,
                question_mode=args.mode,
                mode="live" if args.live else "offline",
                config_path=args.config,
                budget_config_path=args.budget_config,
                live_provider=args.provider,
            )
            qa_result = asyncio.run(qa) if args.live else _run_offline_coro(qa)
        except (VideoAgentError, ValueError, OSError, sqlite3.Error) as exc:
            error = {"code": exc.code.value if isinstance(exc, VideoAgentError) else "QA_FAILED"}
            if isinstance(exc, VideoAgentError):
                for key in ("question_id", "qa_job_id"):
                    if isinstance(value := exc.details.get(key), str):
                        error[key] = value
            print(json.dumps({"error": error}) if args.json else error["code"], file=sys.stderr)
            return 1
        print(json.dumps(qa_result if args.json else qa_result["answer"], ensure_ascii=False))
        return 0

    if args.command == "index":
        if args.retry and not args.analysis_id:
            parser.error("--retry requires the original --analysis-id")
        try:
            coro_index = index_registered_asset(
                data_root=args.data_root,
                asset_id=args.asset_id,
                analysis_id=args.analysis_id,
                profile=args.profile,
                mode="live" if args.live else "offline",
                config_path=args.config,
                budget_config_path=args.budget_config,
                live_provider=args.provider,
                asr_model_path=args.asr_model_path,
                explicit_retry=args.retry,
            )
            indexed = asyncio.run(coro_index) if args.live else _run_offline_coro(coro_index)
        except (VideoAgentError, ValueError, OSError, sqlite3.Error) as exc:
            code = exc.code.value if isinstance(exc, VideoAgentError) else "INDEX_FAILED"
            print(json.dumps({"error": {"code": code}}) if args.json else code, file=sys.stderr)
            return 1
        print(json.dumps(indexed, ensure_ascii=False) if args.json else indexed["analysis_id"])
        return 0
    if args.command == "assets":
        try:
            importer = build_asset_importer(
                args.data_root, load_app_config(args.config), quota_bytes=args.quota_bytes
            )
            asset = importer.import_file(
                args.path, asset_id="asset_" + uuid.uuid4().hex, authorization=args.authorization
            )
        except VideoAgentError as exc:
            if args.json:
                print(json.dumps({"error": exc.code.value, "message": exc.message}))
            else:
                print(str(exc), file=sys.stderr)
            return 1
        print(asset.model_dump_json() if args.json else asset.asset_id)
        return 0

    if args.command == "doctor":
        report = run_doctor(config_path=args.config, data_root=args.data_root)
        if args.json:
            payload = {
                "ok": report.ok,
                "mode": report.mode,
                "checks": [asdict(check) for check in report.checks],
            }
            print(json.dumps(payload, ensure_ascii=False))
        else:
            status_label = "OK" if report.ok else "FAIL"
            print(f"video-agent doctor [{report.mode}]: {status_label}")
            for check in report.checks:
                print(f"  [{check.status.upper():6s}] {check.name}: {check.detail}")
        return 0 if report.ok else 1

    if args.command == "slice":
        run_mode: Literal["offline", "live"] = "live" if args.live else "offline"
        container = build_slice_container(
            mode=run_mode,
            data_root=args.data_root,
            live_provider=args.provider,
            config_path=args.config,
            cloud_egress_authorized=bool(args.cloud_egress_authorized),
            budget_config_path=args.budget_config,
            asr_model_path=args.asr_model_path,
        )
        asset_id = f"asset_{uuid.uuid4().hex[:10]}"
        analysis_id = f"an_{uuid.uuid4().hex[:10]}"
        q_specs = [
            SliceQuestionSpec(question_id=f"q_{idx:02d}", query_text=q_text)
            for idx, q_text in enumerate(args.question, start=1)
        ]
        coro = run_vertical_slice(
            str(args.video),
            asset_id=asset_id,
            analysis_id=analysis_id,
            questions=q_specs,
            media_backend=container.media_backend,
            transcriber=container.transcriber,
            vision_provider=container.vision_provider,
            planner_provider=container.planner_provider,
            budget_guard=container.budget_guard,
            prompt_registry=container.prompt_registry,
            config=container.config,
            clock=container.clock,
            provider_mode=container.mode,
            collect_question_errors=True,
            sample_interval_ms=args.sample_interval_ms,
            max_frames=args.max_frames,
        )
        with closing(container):
            result = asyncio.run(coro) if args.live else _run_offline_coro(coro)
        serialized = _serialize_slice_result(result, q_specs)
        evidence_manifest = container.save_evidence_manifest(result)
        serialized["evidence_manifest"] = evidence_manifest
        if args.json:
            print(json.dumps(serialized, ensure_ascii=False))
        else:
            print(
                f"video-agent slice [{result.provider_mode}]: asset={result.asset.asset_id} "
                f"analysis={result.analysis_id} frames={len(result.frames)} "
                f"audio={result.audio_status}"
            )
            print(f"  evidence_manifest={evidence_manifest} (relative to --data-root)")
            for row in serialized["questions"]:
                answer = row["answer"]
                if answer is None:
                    print(f"  [{row['question_id']}] status=failed error={row['error_code']}")
                else:
                    print(
                        f"  [{row['question_id']}] status={answer['status']} "
                        f"claims={len(answer['claims'])} gaps={len(answer['gaps'])}"
                    )
        return int(bool(result.question_errors))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
