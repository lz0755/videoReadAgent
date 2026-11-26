"""Local D1 freeze audit. Only aggregate counts and content digests leave the evaluator."""

from pathlib import Path
from typing import Any


def verify_packet(videos: list[dict[str,Any]], seed: list[dict[str,Any]], packet: dict[str,Any],
                  *, evidence_root: Path, repo_root: Path) -> None:
    raise NotImplementedError


def audit(*, repo_root: Path, packet_path: Path | None = None) -> dict[str,Any]:
    raise NotImplementedError
