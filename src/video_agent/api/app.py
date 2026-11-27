"""Create the local HTTP service after the caller supplies its settings."""

from pathlib import Path

from fastapi import FastAPI


def create_app(
    data_root: Path, *, token: str, config_path: Path = Path("config/defaults.yaml")
) -> FastAPI:
    raise NotImplementedError("M5 local service")
