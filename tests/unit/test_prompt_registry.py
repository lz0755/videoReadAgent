"""Unit tests for M1.6 PromptRegistry, strict rendering, and bundle/input hashing."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import pytest

from video_agent.application.prompt_registry import PromptRegistry, canonical_json_sha256
from video_agent.domain.errors import ErrorCode, PromptIntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

EXPECTED_TEMPLATE_PATHS: dict[str, str] = {
    "vision_observe": "vision/observe.jinja2",
    "summary_summarize": "summary/summarize.jinja2",
    "planner_decide": "planner/decide.jinja2",
    "answer_compose": "answer/compose.jinja2",
    "repair_schema": "repair/schema.jinja2",
}

TOOL_SCHEMA_V1: dict[str, object] = {
    "tools": ["search_evidence", "inspect_clip", "compare_frames"],
}
OUTPUT_SCHEMA_V1: dict[str, object] = {
    "schema_version": "1.1",
    "required": ["status", "claims", "gaps"],
}


def _copy_prompts_tree(dst: Path) -> Path:
    shutil.copytree(PROMPTS_DIR, dst)
    return dst / "registry.json"


def test_repo_prompt_registry_loads_all_five_templates_and_renders() -> None:
    """Production prompts/ directory contains all 5 verified LF UTF-8 templates."""
    registry = PromptRegistry(PROMPTS_DIR / "registry.json")
    assert set(registry.entries.keys()) == set(EXPECTED_TEMPLATE_PATHS.keys())
    for name, rel_path in EXPECTED_TEMPLATE_PATHS.items():
        entry = registry.entries[name]
        assert entry.relative_path == rel_path
        raw_bytes = (PROMPTS_DIR / rel_path).read_bytes()
        assert not raw_bytes.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw_bytes
        assert entry.template_sha256 == hashlib.sha256(raw_bytes).hexdigest()

    rendered_a = registry.render(
        "answer_compose",
        {
            "question_text": "在哪修改了孔径？",
            "evidence_summary": "ev_1: 孔径=20",
        },
        tool_schema=TOOL_SCHEMA_V1,
        output_schema=OUTPUT_SCHEMA_V1,
    )
    rendered_b = registry.render(
        "answer_compose",
        {
            "question_text": "视频最后发生了什么？",
            "evidence_summary": "ev_2: 结束画面",
        },
        tool_schema=TOOL_SCHEMA_V1,
        output_schema=OUTPUT_SCHEMA_V1,
    )
    # Runtime video/question variables change request_input_digest, NEVER template/bundle hash
    assert rendered_a.template_hash == rendered_b.template_hash
    assert rendered_a.bundle_hash == rendered_b.bundle_hash
    assert rendered_a.request_input_digest != rendered_b.request_input_digest
    assert "在哪修改了孔径？" in rendered_a.rendered_text


def test_one_byte_template_or_schema_change_mutates_bundle_hash(tmp_path: Path) -> None:
    """Changing 1 template byte (same label), tool schema, or output schema mutates hash."""
    reg_path = _copy_prompts_tree(tmp_path / "prompts")
    base_reg = PromptRegistry(reg_path)
    base_hash = base_reg.compute_bundle_hash(
        tool_schema=TOOL_SCHEMA_V1,
        output_schema=OUTPUT_SCHEMA_V1,
    )

    # Tool schema change mutates bundle hash
    tool_changed = base_reg.compute_bundle_hash(
        tool_schema={"tools": ["search_evidence", "inspect_clip"]},
        output_schema=OUTPUT_SCHEMA_V1,
    )
    assert tool_changed != base_hash

    # Output schema change mutates bundle hash
    output_changed = base_reg.compute_bundle_hash(
        tool_schema=TOOL_SCHEMA_V1,
        output_schema={"schema_version": "1.1", "required": ["status", "claims", "gaps", "extra"]},
    )
    assert output_changed != base_hash

    # 1-byte template edit (trailing space) WITHOUT updating registry fails integrity check
    obs_file = tmp_path / "prompts" / "vision" / "observe.jinja2"
    modified_bytes = obs_file.read_bytes() + b" "
    obs_file.write_bytes(modified_bytes)
    with pytest.raises(PromptIntegrityError) as exc_mismatch:
        PromptRegistry(reg_path)
    assert exc_mismatch.value.code is ErrorCode.PROMPT_INTEGRITY_ERROR

    # Updating registry hash while keeping the exact same label produces a NEW bundle hash
    reg_data = json.loads(reg_path.read_text(encoding="utf-8"))
    for item in reg_data["templates"]:
        if item["name"] == "vision_observe":
            item["template_sha256"] = hashlib.sha256(modified_bytes).hexdigest()
    reg_path.write_text(json.dumps(reg_data, indent=2) + "\n", encoding="utf-8")

    updated_reg = PromptRegistry(reg_path)
    updated_hash = updated_reg.compute_bundle_hash(
        tool_schema=TOOL_SCHEMA_V1,
        output_schema=OUTPUT_SCHEMA_V1,
    )
    assert updated_reg.entries["vision_observe"].label == base_reg.entries["vision_observe"].label
    assert updated_hash != base_hash


def test_registry_rejects_crlf_and_bom_and_strict_missing_vars(tmp_path: Path) -> None:
    """Templates with CRLF or UTF-8 BOM are rejected; missing variables fail loudly."""
    reg_crlf = _copy_prompts_tree(tmp_path / "prompts_crlf")
    target_crlf = tmp_path / "prompts_crlf" / "repair" / "schema.jinja2"
    crlf_bytes = target_crlf.read_bytes().replace(b"\n", b"\r\n")
    target_crlf.write_bytes(crlf_bytes)
    reg_data = json.loads(reg_crlf.read_text(encoding="utf-8"))
    for item in reg_data["templates"]:
        if item["name"] == "repair_schema":
            item["template_sha256"] = hashlib.sha256(crlf_bytes).hexdigest()
    reg_crlf.write_text(json.dumps(reg_data, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(PromptIntegrityError) as exc_crlf:
        PromptRegistry(reg_crlf)
    assert exc_crlf.value.code is ErrorCode.PROMPT_INTEGRITY_ERROR

    reg_bom = _copy_prompts_tree(tmp_path / "prompts_bom")
    target_bom = tmp_path / "prompts_bom" / "repair" / "schema.jinja2"
    bom_bytes = b"\xef\xbb\xbf" + target_bom.read_bytes()
    target_bom.write_bytes(bom_bytes)
    reg_bom_data = json.loads(reg_bom.read_text(encoding="utf-8"))
    for item in reg_bom_data["templates"]:
        if item["name"] == "repair_schema":
            item["template_sha256"] = hashlib.sha256(bom_bytes).hexdigest()
    reg_bom.write_text(json.dumps(reg_bom_data, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(PromptIntegrityError) as exc_bom:
        PromptRegistry(reg_bom)
    assert exc_bom.value.code is ErrorCode.PROMPT_INTEGRITY_ERROR

    # Missing template variable fails strictly; NaN rejected in canonical_json_sha256
    reg_ok = PromptRegistry(PROMPTS_DIR / "registry.json")
    with pytest.raises(PromptIntegrityError):
        reg_ok.render(
            "answer_compose",
            {},
            tool_schema=TOOL_SCHEMA_V1,
            output_schema=OUTPUT_SCHEMA_V1,
        )
    with pytest.raises(ValueError):
        canonical_json_sha256({"bad": math.nan})


def test_git_checkout_with_autocrlf_true_preserves_lf_and_prompt_hashes(tmp_path: Path) -> None:
    """M1.6 regression: .gitattributes ensures LF checkout even when core.autocrlf=true."""
    import subprocess

    gitattributes = REPO_ROOT / ".gitattributes"
    assert gitattributes.is_file(), "missing .gitattributes to enforce LF for prompt templates"

    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    shutil.copy2(gitattributes, src_repo / ".gitattributes")
    shutil.copytree(PROMPTS_DIR, src_repo / "prompts")

    git_env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "init"], cwd=src_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.autocrlf", "true"], cwd=src_repo, check=True, capture_output=True
    )
    subprocess.run(["git", "add", ".gitattributes", "prompts"], cwd=src_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init prompts"],
        cwd=src_repo,
        env=git_env,
        check=True,
        capture_output=True,
    )

    clone_dir = tmp_path / "cloned_autocrlf"
    subprocess.run(
        ["git", "clone", "-c", "core.autocrlf=true", str(src_repo), str(clone_dir)],
        check=True,
        capture_output=True,
    )

    for rel_path in EXPECTED_TEMPLATE_PATHS.values():
        checked_out = (clone_dir / "prompts" / rel_path).read_bytes()
        assert b"\r" not in checked_out, f"CRLF leaked into {rel_path} on autocrlf=true checkout"

    cloned_registry = PromptRegistry(clone_dir / "prompts" / "registry.json")
    assert set(cloned_registry.entries.keys()) == set(EXPECTED_TEMPLATE_PATHS.keys())
