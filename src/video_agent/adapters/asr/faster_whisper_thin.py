"""Thin M1.5.1 local faster-whisper Transcriber adapter.

Never loads model weights or accesses network at import time.
Strictly enforces vad_filter=False and local_files_only=True.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import TimeRangeMs, TranscriptSegment
from video_agent.domain.ports import TranscribeRequest, TranscribeResponse


class FasterWhisperThinTranscriber:
    """Thin offline-safe faster-whisper adapter implementing Transcriber."""

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        model_path: Path | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        asr_version: str = "faster-whisper-thin-v1",
    ) -> None:
        self._artifact_store = artifact_store
        self._model_path = model_path
        self._device = device
        self._compute_type = compute_type
        self._asr_version = asr_version
        self._model: Any = None

    def _get_or_load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_path is None or not self._model_path.is_dir():
            raise MediaError(
                ErrorCode.DEPENDENCY_MISSING,
                (
                    "local faster-whisper model directory is missing; implicit model downloads "
                    "are forbidden (pass an explicit pre-downloaded model directory)"
                ),
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MediaError(
                ErrorCode.DEPENDENCY_MISSING,
                f"faster-whisper package is not installed: {exc}",
            ) from exc

        try:
            self._model = WhisperModel(
                str(self._model_path),
                device=self._device,
                compute_type=self._compute_type,
                local_files_only=True,
            )
        except Exception as exc:
            raise MediaError(
                ErrorCode.DEPENDENCY_MISSING,
                f"failed to load local faster-whisper model from directory: {type(exc).__name__}",
            ) from exc
        return self._model

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        if request.vad_filter is not False:
            raise MediaError(
                ErrorCode.CAPABILITY_MISMATCH,
                "P0 ASR strictly requires vad_filter=False",
            )

        model = self._get_or_load_model()
        audio_path = self._artifact_store.resolve_path(request.audio_artifact_id)

        try:
            # FFmpeg emits 16 kHz mono PCM. Passing samples avoids a second PyAV decode.
            with wave.open(str(audio_path), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                    raise ValueError("expected mono 16-bit 16 kHz PCM")
                # FFmpeg's pipe output has an open-ended RIFF data size.
                pcm = wav.readframes(min(wav.getnframes(), audio_path.stat().st_size // 2))
            if not pcm:
                raise ValueError("empty PCM audio")
            waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / np.float32(32768)
            raw_segments, info = model.transcribe(
                waveform,
                language=request.language,
                vad_filter=False,
            )
            materialized = list(raw_segments)
        except Exception as exc:
            raise MediaError(
                ErrorCode.CORRUPT_MEDIA,
                f"faster-whisper transcription failed: {type(exc).__name__}",
            ) from exc

        timeline = request.audio_timeline
        offset_ms = 0 if timeline.leading_silence_padded else timeline.audio_offset_ms
        detected_lang = getattr(info, "language", None) or request.language

        segments: list[TranscriptSegment] = []
        for idx, seg in enumerate(materialized, start=1):
            text = str(getattr(seg, "text", "")).strip()
            if not text:
                continue
            local_start_ms = math.floor(float(seg.start) * 1000.0)
            local_end_ms = math.floor(float(seg.end) * 1000.0)
            start_ms = max(0, timeline.extracted_start_ms + offset_ms + local_start_ms)
            end_ms = max(start_ms + 1, timeline.extracted_start_ms + offset_ms + local_end_ms)
            segments.append(
                TranscriptSegment(
                    evidence_id=f"ev_asr_{idx:04d}_{start_ms}ms",
                    asset_id=request.asset_id,
                    time_range=TimeRangeMs(start_ms=start_ms, end_ms=end_ms),
                    text=text,
                    asr_version=self._asr_version,
                    audio_timeline=timeline,
                    language=str(detected_lang) if detected_lang is not None else None,
                )
            )

        return TranscribeResponse(
            segments=segments,
            asr_version=self._asr_version,
            language=str(detected_lang) if detected_lang is not None else None,
        )
