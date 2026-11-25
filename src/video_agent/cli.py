"""CLI entrypoint for video-agent (M1.1 offline doctor and command skeleton)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from video_agent.domain.config import AppConfig


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-agent",
        description="Local authorized video understanding agent CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
