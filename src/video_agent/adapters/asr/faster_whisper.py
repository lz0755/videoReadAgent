"""Offline faster-whisper in a supervised persistent spawn process."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from video_agent.domain.ports import TranscribeRequest, TranscribeResponse

if TYPE_CHECKING:
    from video_agent.adapters.media.thin_media import LocalArtifactStore


def _load_model(model_path: Path, device: str, compute_type: str) -> Any:
    raise NotImplementedError


class FasterWhisperTranscriber:
    def __init__(
        self,
        store: LocalArtifactStore,
        *,
        model_path: Path | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        timeout_seconds: float = 120.0,
        cancelled: Callable[[], bool] | None = None,
        duration_for_asset: Callable[[str], int] | None = None,
    ) -> None:
        pass

    @property
    def worker_pid(self) -> int | None:
        raise NotImplementedError

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
