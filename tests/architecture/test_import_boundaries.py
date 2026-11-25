"""AST supplementary architecture gate and import-linter verification (M1.1).

Enforces strict dependency boundaries across domain, application, and agent:
- Resolves relative imports and inspects TYPE_CHECKING branches.
- Blocks forbidden internal layers, infrastructure stdlib modules, third-party
  SDK/web/media packages (even when not installed), and dynamic import bypasses.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "video_agent"

FORBIDDEN_EXTERNAL_MODULES: frozenset[str] = frozenset(
    {
        "aiohttp",
        "av",
        "ctranslate2",
        "cv2",
        "fastapi",
        "faster_whisper",
        "google",
        "httpx",
        "openai",
        "PIL",
        "portalocker",
        "requests",
        "scenedetect",
        "starlette",
        "streamlit",
        "torch",
        "urllib3",
    }
)

FORBIDDEN_STDLIB_INFRA: frozenset[str] = frozenset(
    {
        "ctypes",
        "ftplib",
        "http",
        "multiprocessing",
        "smtplib",
        "socket",
        "sqlite3",
        "ssl",
        "subprocess",
        "urllib",
    }
)

FORBIDDEN_DYNAMIC_MODULES: frozenset[str] = frozenset(
    {
        "importlib",
        "pkgutil",
        "runpy",
    }
)

FORBIDDEN_DYNAMIC_BUILTINS: frozenset[str] = frozenset(
    {
        "__import__",
        "compile",
        "eval",
        "exec",
    }
)

LAYER_FORBIDDEN_INTERNAL: dict[str, frozenset[str]] = {
    "domain": frozenset(
        {
            "video_agent.application",
            "video_agent.agent",
            "video_agent.adapters",
            "video_agent.api",
            "video_agent.ui",
            "video_agent.bootstrap",
            "video_agent.cli",
            "video_agent.worker",
        }
    ),
    "application": frozenset(
        {
            "video_agent.adapters",
            "video_agent.api",
            "video_agent.ui",
            "video_agent.bootstrap",
            "video_agent.cli",
            "video_agent.worker",
        }
    ),
    "agent": frozenset(
        {
            "video_agent.adapters",
            "video_agent.api",
            "video_agent.ui",
            "video_agent.bootstrap",
            "video_agent.cli",
            "video_agent.worker",
        }
    ),
}


def _resolve_relative_import(
    current_module: str,
    *,
    is_package_init: bool,
    module: str | None,
    level: int,
) -> str:
    """Resolve an ImportFrom node with level > 0 to a dotted module name."""
    parts = current_module.split(".")
    package_parts = parts if is_package_init else parts[:-1]
    if level > len(package_parts):
        return "<invalid-relative-import>"
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    if module:
        return ".".join([*base_parts, *module.split(".")])
    return ".".join(base_parts)


def check_source_boundaries(
    source: str,
    *,
    module_name: str,
    layer: str,
    is_package_init: bool = False,
) -> list[str]:
    """Return a list of architecture boundary violations for a Python source module."""
    tree = ast.parse(source, filename=module_name)
    violations: list[str] = []
    forbidden_internal = LAYER_FORBIDDEN_INTERNAL[layer]

    def _check_target(target: str, lineno: int) -> None:
        top_level = target.split(".", maxsplit=1)[0]
        if top_level in FORBIDDEN_EXTERNAL_MODULES:
            violations.append(f"{module_name}:{lineno}: forbidden external import '{target}'")
        if top_level in FORBIDDEN_STDLIB_INFRA:
            violations.append(f"{module_name}:{lineno}: forbidden stdlib infra import '{target}'")
        if top_level in FORBIDDEN_DYNAMIC_MODULES:
            violations.append(f"{module_name}:{lineno}: forbidden dynamic import module '{target}'")
        for prefix in forbidden_internal:
            if target == prefix or target.startswith(prefix + "."):
                violations.append(
                    f"{module_name}:{lineno}: layer '{layer}' cannot import '{target}'"
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_target(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                resolved = _resolve_relative_import(
                    module_name,
                    is_package_init=is_package_init,
                    module=node.module,
                    level=node.level,
                )
                if resolved == "<invalid-relative-import>":
                    violations.append(f"{module_name}:{node.lineno}: invalid relative import")
                    continue
                _check_target(resolved, node.lineno)
                if node.module is None:
                    for alias in node.names:
                        _check_target(f"{resolved}.{alias.name}", node.lineno)
            elif node.module is not None:
                _check_target(node.module, node.lineno)
                for alias in node.names:
                    _check_target(f"{node.module}.{alias.name}", node.lineno)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_DYNAMIC_BUILTINS:
            violations.append(f"{module_name}:{node.lineno}: forbidden dynamic builtin '{node.id}'")
        elif isinstance(node, ast.Attribute) and node.attr in {
            "__import__",
            "import_module",
            "spec_from_file_location",
            "module_from_spec",
            "exec_module",
        }:
            violations.append(
                f"{module_name}:{node.lineno}: forbidden dynamic import attribute '{node.attr}'"
            )

    return violations


def _module_name_for_path(path: Path) -> tuple[str, bool]:
    rel = path.relative_to(REPO_ROOT / "src")
    is_init = rel.name == "__init__.py"
    parts = list(rel.parts[:-1]) if is_init else list(rel.with_suffix("").parts)
    return ".".join(parts), is_init


def test_real_core_layers_pass_ast_boundary_gate() -> None:
    """Every module in domain, application, and agent must have zero AST violations."""
    all_violations: list[str] = []
    for layer in ("domain", "application", "agent"):
        layer_dir = SRC_ROOT / layer
        assert layer_dir.is_dir(), f"missing layer package directory: {layer_dir}"
        for py_file in sorted(layer_dir.rglob("*.py")):
            mod_name, is_init = _module_name_for_path(py_file)
            source = py_file.read_text(encoding="utf-8")
            all_violations.extend(
                check_source_boundaries(
                    source,
                    module_name=mod_name,
                    layer=layer,
                    is_package_init=is_init,
                )
            )
    assert all_violations == [], "AST boundary violations:\n" + "\n".join(all_violations)


def test_ast_gate_allows_valid_relative_import() -> None:
    """Fixture 1: legal intra-layer relative import passes."""
    src = "from .errors import DomainError\n"
    violations = check_source_boundaries(
        src,
        module_name="video_agent.domain.models",
        layer="domain",
    )
    assert violations == []


def test_ast_gate_blocks_forbidden_relative_import() -> None:
    """Fixture 2: cross-layer relative import into adapters fails."""
    src = "from ..adapters.storage import connection\n"
    violations = check_source_boundaries(
        src,
        module_name="video_agent.domain.models",
        layer="domain",
    )
    assert any("video_agent.adapters" in item for item in violations)


def test_ast_gate_blocks_type_checking_forbidden_import() -> None:
    """TYPE_CHECKING branch cannot smuggle forbidden imports into core layers."""
    src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import sqlite3\n"
    violations = check_source_boundaries(
        src,
        module_name="video_agent.application.service",
        layer="application",
    )
    assert any("sqlite3" in item for item in violations)


def test_ast_gate_blocks_aliased_dynamic_import() -> None:
    """Fixture 3: aliased importlib and __import__ bypasses fail."""
    src_importlib = "import importlib as il\nmod = il.import_module('sqlite3')\n"
    v1 = check_source_boundaries(
        src_importlib,
        module_name="video_agent.agent.controller",
        layer="agent",
    )
    assert any("importlib" in item for item in v1)
    assert any("import_module" in item for item in v1)

    src_dunder = "loader = __import__\nloader('openai')\n"
    v2 = check_source_boundaries(
        src_dunder,
        module_name="video_agent.domain.models",
        layer="domain",
    )
    assert any("__import__" in item for item in v2)


def test_ast_gate_blocks_uninstalled_external_package() -> None:
    """Fixture 4: forbidden external package is blocked even when not installed."""
    src = "import faster_whisper\nimport fastapi\n"
    violations = check_source_boundaries(
        src,
        module_name="video_agent.domain.models",
        layer="domain",
    )
    assert any("faster_whisper" in item for item in violations)
    assert any("fastapi" in item for item in violations)


def test_import_linter_contracts_pass() -> None:
    """Primary import-linter gate passes on the installed video_agent package."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"lint-imports failed:\n{result.stdout}\n{result.stderr}"
    assert "2 kept, 0 broken" in result.stdout
