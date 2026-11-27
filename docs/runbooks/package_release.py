"""Build a reproducible local source ZIP from the public repository files."""

import argparse
import hashlib
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = (
    ".gitattributes",
    ".github",
    ".python-version",
    ".streamlit/config.toml",
    "AGENTS.md",
    "README.md",
    "Dockerfile.ci",
    "pyproject.toml",
    "uv.lock",
    "src",
    "config",
    "prompts",
    "eval",
    "experiments",
    "tests",
    "docs",
)
# Allows packaging the approved M6 working tree before its first commit, without
# picking up local approval files, evaluator answers or media from untracked paths.
M6 = (
    "config/examples/m6-budget.yaml",
    "docs/runbooks/package_release.py",
    "docs/runbooks/evaluation.md",
    "docs/runbooks/security.md",
    "docs/handoff/PR-M6.md",
    "docs/handoff/PR-M6.1a.md",
    "docs/handoff/PR-M6.1b.md",
    "docs/handoff/PR-M6.2.md",
    "docs/handoff/PR-M6.3.md",
    "eval/metrics.py",
    "eval/report.py",
    "eval/runner.py",
    "eval/manifests/dev.jsonl",
    "eval/manifests/heldout.jsonl",
    "eval/manifests/heldout_events.jsonl",
    "tests/unit/test_eval_metrics.py",
    "tests/unit/test_eval_release.py",
    "tests/unit/test_eval_usage_groups.py",
    "tests/unit/test_eval_publication.py",
    "tests/integration/test_eval_runner.py",
    "tests/contract/test_m6_replay.py",
)


def pack(output: Path) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *PUBLIC], cwd=ROOT, check=True, capture_output=True
    )
    paths = sorted(set(result.stdout.decode().strip("\0").split("\0")) | set(M6))
    output.parent.mkdir(exist_ok=True, parents=True)
    sums = []
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:

        def write(name: str, data: bytes) -> None:
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=ZIP_DEFLATED)

        for name in paths:
            path = ROOT / name
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(ROOT):
                raise ValueError("source member escaped the repository")
            data = path.read_bytes()
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
            write(name, data)
        write("SHA256SUMS", "".join(sums).encode())
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(pack(parser.parse_args().output))
