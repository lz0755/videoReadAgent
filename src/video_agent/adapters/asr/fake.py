"""Deterministic offline Fake ASR Transcriber adapter (M1.3)."""

from __future__ import annotations

from typing import Literal

from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import TimeRangeMs, TranscriptSegment
from video_agent.domain.ports import TranscribeRequest, TranscribeResponse

FakeAsrScenario = Literal[
    "normal",
    "no_speech",
    "no_audio",
    "corrupt_audio",
    "timeout",
    "dependency_missing",
]


class FakeTranscriber:
    """Deterministic async Fake implementing Transcriber without loading model weights."""

    def __init__(
        self,
        *,
        scenario: FakeAsrScenario = "normal",
        asr_version: str = "fake-asr-v1",
    ) -> None:
        self.calls: list[TranscribeRequest] = []
        self._scenario = scenario
        self._asr_version = asr_version

    async def transcribe(self, request: TranscribeRequest) -> TranscribeResponse:
        call_idx = len(self.calls)
        self.calls.append(request)

        if self._scenario == "no_audio":
            raise MediaError(ErrorCode.NO_AUDIO_STREAM, "asset has no audio stream")
        if self._scenario == "corrupt_audio":
            raise MediaError(ErrorCode.CORRUPT_MEDIA, "audio stream failed decoding")
        if self._scenario == "timeout":
            raise MediaError(ErrorCode.PROVIDER_TIMEOUT, "local ASR subprocess timed out")
        if self._scenario == "dependency_missing":
            raise MediaError(
                ErrorCode.DEPENDENCY_MISSING,
                "local ASR model directory or runtime dependency is missing",
            )
        if self._scenario == "no_speech":
            return TranscribeResponse(
                segments=[],
                asr_version=self._asr_version,
                language=request.language,
            )

        offset = (
            0
            if request.audio_timeline.leading_silence_padded
            else max(0, request.audio_timeline.audio_offset_ms)
        )
        start_ms = request.audio_timeline.extracted_start_ms + offset
        end_ms = start_ms + 2000

        segment = TranscriptSegment(
            evidence_id=f"ev_asr_fake_{call_idx + 1:04d}",
            asset_id=request.asset_id,
            time_range=TimeRangeMs(start_ms=start_ms, end_ms=end_ms),
            text="示例语音：在这里修改孔径参数为 20。",
            asr_version=self._asr_version,
            audio_timeline=request.audio_timeline,
            language=request.language or "zh",
        )
        return TranscribeResponse(
            segments=[segment],
            asr_version=self._asr_version,
            language=request.language or "zh",
        )
