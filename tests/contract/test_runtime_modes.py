"""Contract tests for M1.1 runtime modes, offline doctor, config, and isolation files."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from video_agent.cli import main, run_doctor

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_import_all_skeleton_packages_offline_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing package modules requires no API keys, network, or model weights."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    modules = (
        "video_agent",
        "video_agent.domain",
        "video_agent.application",
        "video_agent.agent",
        "video_agent.adapters",
        "video_agent.adapters.media",
        "video_agent.adapters.asr",
        "video_agent.adapters.llm",
        "video_agent.adapters.storage",
        "video_agent.adapters.storage.migrations",
        "video_agent.api",
        "video_agent.ui",
        "video_agent.bootstrap",
        "video_agent.worker",
        "video_agent.cli",
    )
    for mod in modules:
        imported = importlib.import_module(mod)
        assert imported is not None


def test_default_config_files_exist_and_hold_p0_guardrails() -> None:
    """config/defaults.yaml and config/prices.yaml exist with strict P0 guardrails."""
    defaults_path = REPO_ROOT / "config" / "defaults.yaml"
    prices_path = REPO_ROOT / "config" / "prices.yaml"
    assert defaults_path.is_file(), "missing config/defaults.yaml"
    assert prices_path.is_file(), "missing config/prices.yaml"

    defaults: dict[str, Any] = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    prices: dict[str, Any] = yaml.safe_load(prices_path.read_text(encoding="utf-8"))

    assert defaults["media"]["max_duration_ms"] == 600_000
    assert defaults["media"]["max_file_bytes"] == 524_288_000
    assert defaults["ingest"]["copy_chunk_bytes"] == 8_388_608
    assert defaults["ui"]["max_upload_bytes"] == 67_108_864
    assert defaults["asr"]["vad_filter"] is False

    assert defaults["analysis"]["core_window_ms"] == 20_000
    assert defaults["analysis"]["context_overlap_ms"] == 2_000
    assert defaults["analysis"]["base_fps"] == 0.2
    assert defaults["analysis"]["max_unique_sampled_frames"] == 192
    assert defaults["analysis"]["max_submitted_image_parts"] == 720
    assert defaults["analysis"]["max_images_per_request"] == 8
    assert defaults["analysis"]["max_provider_attempts"] == 90
    assert defaults["analysis"]["deadline_seconds"] == 3600

    assert defaults["qa"]["max_rounds"] == 5
    assert defaults["qa"]["max_tool_calls"] == 6
    assert defaults["qa"]["max_expensive_tool_calls"] == 2
    assert defaults["qa"]["max_submitted_image_parts"] == 32
    assert defaults["qa"]["max_images_per_request"] == 16
    assert defaults["qa"]["max_provider_attempts"] == 10
    assert defaults["qa"]["deadline_seconds"] == 180
    assert defaults["qa"]["max_clip_ms"] == 8_000
    assert defaults["qa"]["inspect_fps"] == 2

    assert defaults["provider"]["sdk_max_retries"] == 0
    assert defaults["provider"]["tool_return_mode"] == "observations_text"
    assert defaults["provider_policy"]["request_timeout_seconds"] == 45
    assert defaults["provider_policy"]["max_transient_retries"] == 2
    assert defaults["provider_policy"]["max_schema_repairs"] == 1
    assert defaults["privacy"]["cloud_upload_enabled"] is False
    assert defaults["privacy"]["log_sensitive_content"] is False
    assert "api_key" not in defaults["provider"]

    assert prices["currency"] == "USD"
    assert prices["effective_date"]
    assert prices["source"]
    assert "deepseek-flash" in prices["models"]


def test_offline_doctor_succeeds_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Offline doctor passes with missing API keys reported as notice, not failure."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    report = run_doctor(
        config_path=REPO_ROOT / "config" / "defaults.yaml",
        data_root=tmp_path / "data_root",
    )
    assert report.ok is True
    assert report.mode == "offline"

    by_name = {check.name: check for check in report.checks}
    assert by_name["python"].status == "ok"
    assert by_name["ffmpeg"].status == "ok"
    assert by_name["ffprobe"].status == "ok"
    assert by_name["sqlite_fts5"].status == "ok"
    assert by_name["config"].status == "ok"
    assert by_name["data_root"].status == "ok"
    assert by_name["cloud_credentials"].status == "notice"


def test_cli_doctor_json_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`video-agent doctor --json` exits 0 and emits structured JSON without leaking keys."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-do-not-leak-123456")
    exit_code = main(
        [
            "doctor",
            "--json",
            "--config",
            str(REPO_ROOT / "config" / "defaults.yaml"),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "sk-secret-do-not-leak-123456" not in captured.out
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["mode"] == "offline"
    assert isinstance(payload["checks"], list)


def test_isolation_artifacts_present_and_enforced() -> None:
    """Dockerfile.ci, CI workflow, and OS network canary exist with required guards."""
    dockerfile = REPO_ROOT / "Dockerfile.ci"
    workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    canary = REPO_ROOT / "tests" / "canary" / "network_denied.py"

    assert dockerfile.is_file(), "missing Dockerfile.ci"
    assert workflow.is_file(), "missing .github/workflows/ci.yml"
    assert canary.is_file(), "missing tests/canary/network_denied.py"

    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    assert "ffmpeg" in dockerfile_text
    assert "curl" in dockerfile_text
    assert "uv sync --frozen" in dockerfile_text

    workflow_text = workflow.read_text(encoding="utf-8")
    assert "--no-sync" in workflow_text
    assert "--disable-socket" in workflow_text
    assert "--network none" in workflow_text
    assert "HF_HUB_OFFLINE=1" in workflow_text
    assert "TRANSFORMERS_OFFLINE=1" in workflow_text
    assert "tests/canary/network_denied.py" in workflow_text
    assert "lint-imports" in workflow_text
    # CI format gate scope must match AGENTS.md (`src experiments tests`)
    assert "ruff format --check src experiments tests" in workflow_text
    # Dockerfile.ci must copy all files needed for offline isolation tests inside the container
    assert "COPY Dockerfile.ci" in dockerfile_text
    assert "COPY .github" in dockerfile_text
    assert ".gitattributes" in dockerfile_text


def test_doctor_rejects_invalid_config_consistently_with_strict_app_config(
    tmp_path: Path,
) -> None:
    """M1.2/M1.1 regression: doctor and AppConfig must agree on invalid config (e.g. vad_filter)."""
    raw_defaults: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "config" / "defaults.yaml").read_text(encoding="utf-8")
    )
    bad_cfg = {**raw_defaults, "asr": {"vad_filter": True}}
    bad_cfg_path = tmp_path / "bad_defaults.yaml"
    bad_cfg_path.write_text(yaml.safe_dump(bad_cfg), encoding="utf-8")

    report = run_doctor(config_path=bad_cfg_path, data_root=tmp_path / "data_root")
    assert report.ok is False
    by_name = {check.name: check for check in report.checks}
    assert by_name["config"].status == "fail"
    assert main(["doctor", "--config", str(bad_cfg_path), "--data-root", str(tmp_path / "dr")]) == 1
