"""Regression for a caller presenting another asset's intact input artifact."""

from pathlib import Path

import pytest

from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from tests.integration.test_media_cache import cached_media
from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.storage.media_cache import FileMediaCache
from video_agent.application.media_cache import CachedTranscriber
from video_agent.cli import _run_offline_coro
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import TimeRangeMs
from video_agent.domain.ports import TranscribeRequest


@pytest.mark.parametrize("input_kind", ["source", "wav"])
def test_warm_cache_refuses_foreign_input_before_reuse(tmp_path: Path, input_kind: str) -> None:
    path = tmp_path / "checked.mp4"
    generate_f01_cfr(path)
    assert self_check_f01_cfr(path).passed
    store, counted, cache = cached_media(tmp_path)
    owner = cache.probe_video(str(path), asset_id="owner")
    other = cache.probe_video(str(path), asset_id="other")
    if input_kind == "source":
        window = TimeRangeMs(start_ms=0, end_ms=400)
        assert cache.extract_frames(owner, [40], range_ms=window)
        switched = other.model_copy(update={"source_artifact_id": owner.source_artifact_id})
        with pytest.raises(VideoAgentError) as caught:
            cache.extract_frames(switched, [40], range_ms=window)
        assert counted.frame_calls == 1
    else:
        wav, timeline = cache.extract_audio(owner)
        fake = FakeTranscriber()
        asr = CachedTranscriber(
            fake,
            FileMediaCache(tmp_path / "media-cache", store),
            profile=lambda: {"asr_version": "fake-v1"},
        )
        request = TranscribeRequest(
            asset_id=owner.asset_id, audio_artifact_id=wav, audio_timeline=timeline
        )
        assert _run_offline_coro(asr.transcribe(request)).segments
        with pytest.raises(VideoAgentError) as caught:
            _run_offline_coro(
                asr.transcribe(request.model_copy(update={"asset_id": other.asset_id}))
            )
        assert len(fake.calls) == 1
    assert caught.value.code is ErrorCode.CROSS_ASSET_REFERENCE
