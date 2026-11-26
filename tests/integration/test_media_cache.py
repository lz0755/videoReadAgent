"""M2.4 cache behavior through real media ports, disk files and a counted ASR fake."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures.p0_media import build_p0_suite
from video_agent.adapters.asr.fake import FakeTranscriber
from video_agent.adapters.media.thin_media import LocalArtifactStore, PyAVMediaBackend
from video_agent.adapters.storage.media_cache import FileMediaCache, model_directory_digest
from video_agent.application.media_cache import CachedMediaBackend, CachedTranscriber
from video_agent.cli import _run_offline_coro
from video_agent.domain.errors import ErrorCode, MediaError
from video_agent.domain.models import AudioTimelineMap, FrameEvidence, TimeRangeMs, VideoAsset
from video_agent.domain.ports import TranscribeRequest


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("cache-fixtures")
    entries = build_p0_suite(root, root / "checked.json")["fixtures"]
    assert len(entries) == 8 and all(e["passed"] for e in entries.values())
    return {fid: root / entry["file_path"] for fid, entry in entries.items()}


class CountedMedia(PyAVMediaBackend):
    """Counts calls at MediaBackend; every call still runs the real decoder/extractor."""

    def __init__(self, store: LocalArtifactStore, edge: int = 320) -> None:
        super().__init__(store, max_long_edge_px=edge)
        self.frame_calls = 0
        self.audio_calls = 0

    def extract_frames(
        self, asset: VideoAsset, requested_times_ms: list[int], *, range_ms: TimeRangeMs
    ) -> list[FrameEvidence]:
        self.frame_calls += 1
        return super().extract_frames(asset, requested_times_ms, range_ms=range_ms)

    def extract_audio(self, asset: VideoAsset) -> tuple[str, AudioTimelineMap]:
        self.audio_calls += 1
        return super().extract_audio(asset)


def cached_media(
    root: Path, *, edge: int = 320, audio_version: str = "pcm-v1"
) -> tuple[LocalArtifactStore, CountedMedia, CachedMediaBackend]:
    store = LocalArtifactStore(root / "artifacts")
    backend = CountedMedia(store, edge)
    cache = FileMediaCache(root / "media-cache", store)
    return (
        store,
        backend,
        CachedMediaBackend(
            backend,
            cache,
            frame_profile={"processor": "pyav-jpeg-v1", "long_edge_px": edge},
            audio_profile={"processor": audio_version, "sample_rate": 16_000},
        ),
    )


def test_persistent_frames_bind_owners_and_keep_time_evidence(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    source = media["F05"]
    store, backend, cached = cached_media(tmp_path)
    asset = cached.probe_video(str(source), asset_id="asset-a")
    window = TimeRangeMs(start_ms=0, end_ms=200)
    old = cached.extract_frames(asset, [0, 40], range_ms=window)
    assert backend.frame_calls == 1
    assert [f.actual_pts_ms for f in old] == [0, 40] and old[0].raw_pts != old[1].raw_pts
    assert old[0].evidence_id != old[1].evidence_id
    assert store.read_bytes(old[0].artifact_id) == store.read_bytes(old[1].artifact_id)

    reopened, second, cache2 = cached_media(tmp_path)
    assert cache2.extract_frames(asset, [0, 40], range_ms=window) == old
    renamed = tmp_path / "renamed.mp4"
    renamed.write_bytes(source.read_bytes())
    other = cache2.probe_video(str(renamed), asset_id="asset-b")
    new = cache2.extract_frames(other, [0, 40], range_ms=window)
    assert cache2.extract_frames(other, [0, 40], range_ms=window) == new
    assert second.frame_calls == 0
    assert [f.actual_pts_ms for f in new] == [0, 40]
    assert all(f.asset_id == other.asset_id for f in new)
    assert {f.evidence_id for f in old}.isdisjoint(f.evidence_id for f in new)
    for before, after in zip(old, new, strict=True):
        assert before.artifact_id != after.artifact_id
        assert reopened.resolve_path(after.artifact_id).parent.name == other.asset_id
        assert reopened.read_bytes(after.artifact_id) == store.read_bytes(before.artifact_id)


def test_media_changes_and_materialized_frame_damage_force_recomputation(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    store, counted, cache = cached_media(tmp_path)
    window = TimeRangeMs(start_ms=0, end_ms=240)
    asset = cache.probe_video(str(media["F01"]), asset_id="first")
    first = cache.extract_frames(asset, [10], range_ms=window)[0]
    expected = store.read_bytes(first.artifact_id)
    store.resolve_path(first.artifact_id).unlink()
    restored = cache.extract_frames(asset, [10], range_ms=window)[0]
    assert counted.frame_calls == 2 and store.read_bytes(restored.artifact_id) == expected
    store.resolve_path(restored.artifact_id).write_bytes(b"tampered")
    restored = cache.extract_frames(asset, [10], range_ms=window)[0]
    assert counted.frame_calls == 3 and store.read_bytes(restored.artifact_id) == expected
    _, new_backend, resized = cached_media(tmp_path, edge=96)
    small = resized.extract_frames(asset, [10], range_ms=window)[0]
    assert new_backend.frame_calls == 1 and store.read_bytes(small.artifact_id) != expected
    resized.extract_frames(asset, [10], range_ms=TimeRangeMs(start_ms=0, end_ms=100))
    different = resized.probe_video(str(media["F03"]), asset_id="second")
    frame = resized.extract_frames(different, [10], range_ms=window)[0]
    assert new_backend.frame_calls == 3 and frame.origin_pts > 0
    assert asset.source_artifact_id is not None
    store.resolve_path(asset.source_artifact_id).write_bytes(b"changed managed source")
    with pytest.raises(MediaError) as error:
        cache.extract_frames(asset, [10], range_ms=window)
    assert error.value.code is ErrorCode.CORRUPT_MEDIA


def test_audio_reuse_ownership_configuration_and_missing_or_corrupt_wav(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    store, counted, cached = cached_media(tmp_path)
    first = cached.probe_video(str(media["F04"]), asset_id="first")
    second = cached.probe_video(str(media["F04"]), asset_id="second")
    old, timeline = cached.extract_audio(first)
    new, rebound = cached.extract_audio(second)
    expected = hashlib.sha256(store.read_bytes(old)).hexdigest()
    assert counted.audio_calls == 1 and rebound == timeline and rebound.audio_offset_ms > 0
    assert new != old and store.resolve_path(new).parent.name == second.asset_id
    assert store.verify_hash(new, expected)
    store.resolve_path(old).write_bytes(b"invalid RIFF")
    fixed, same = cached.extract_audio(first)
    assert counted.audio_calls == 2 and same == timeline and store.verify_hash(fixed, expected)
    store.resolve_path(fixed).unlink()
    cached.extract_audio(first)
    assert counted.audio_calls == 3
    _, new_counter, changed = cached_media(tmp_path, audio_version="pcm-v2")
    changed.extract_audio(first)
    assert new_counter.audio_calls == 1


def test_asr_uses_audio_contents_model_runtime_and_mapping_and_rebinds_segments(
    tmp_path: Path, media: dict[str, Path]
) -> None:
    store, _, backend = cached_media(tmp_path)
    first = backend.probe_video(str(media["F01"]), asset_id="first")
    other = backend.probe_video(str(media["F01"]), asset_id="other")
    audio1, timeline = backend.extract_audio(first)
    audio2, _ = backend.extract_audio(other)
    store_on_disk = FileMediaCache(tmp_path / "media-cache", store)
    config = {"weights": "digest-1", "compute_type": "int8", "mapping": "original-v1"}
    fake = FakeTranscriber()
    asr = CachedTranscriber(fake, store_on_disk, profile=lambda: config)
    req1 = TranscribeRequest(asset_id="first", audio_artifact_id=audio1, audio_timeline=timeline)
    req2 = req1.model_copy(update={"asset_id": "other", "audio_artifact_id": audio2})
    orig = _run_offline_coro(asr.transcribe(req1))
    same = _run_offline_coro(asr.transcribe(req1))
    assert same == orig and len(fake.calls) == 1
    rebound = _run_offline_coro(asr.transcribe(req2))
    assert len(fake.calls) == 1
    assert rebound.segments[0].asset_id == "other"
    assert rebound.segments[0].evidence_id != orig.segments[0].evidence_id
    assert rebound.segments[0].time_range == orig.segments[0].time_range
    moved = timeline.model_copy(update={"extracted_start_ms": 80_000})
    shifted = _run_offline_coro(asr.transcribe(req2.model_copy(update={"audio_timeline": moved})))
    assert shifted.segments[0].time_range.start_ms == 80_000 and len(fake.calls) == 2
    config["weights"] = "digest-2"
    _run_offline_coro(asr.transcribe(req1))
    config["compute_type"] = "float32"
    _run_offline_coro(asr.transcribe(req1))
    cfg_req = req1.model_copy(update={"language": "en"})
    _run_offline_coro(asr.transcribe(cfg_req))
    config["mapping"] = "original-v2"
    _run_offline_coro(asr.transcribe(cfg_req))
    assert len(fake.calls) == 6
    changed = backend.probe_video(str(media["F04"]), asset_id="changed")
    audio3, _ = backend.extract_audio(changed)
    assert store.read_bytes(audio3) != store.read_bytes(audio1)
    _run_offline_coro(
        asr.transcribe(
            cfg_req.model_copy(update={"audio_artifact_id": audio3, "asset_id": "changed"})
        )
    )
    assert len(fake.calls) == 7


def test_cache_records_are_atomic_and_bound_to_key(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    c = FileMediaCache(root, LocalArtifactStore(tmp_path / "artifacts"))
    a, b = "a" * 64, "b" * 64
    c.write(a, b"first")
    (file_a,) = (p for p in root.rglob("*") if p.is_file())
    c.write(b, b"second")
    (file_b,) = (p for p in root.rglob("*") if p.is_file() and p != file_a)
    file_a.write_bytes(file_b.read_bytes())
    assert c.read(a) is None and c.read(b) == b"second"
    file_b.write_bytes(b"incomplete")
    assert c.read(b) is None
    with ThreadPoolExecutor(max_workers=3) as pool:
        vals = [bytes([n]) * 8192 for n in range(8)]
        list(pool.map(lambda val: c.write(b, val), vals))
    assert c.read(b) in vals
    assert {p for p in root.rglob("*") if p.is_file()} == {file_a, file_b}


def test_model_fingerprint_tracks_content_at_same_path(tmp_path: Path) -> None:
    model = tmp_path / "local-model"
    model.mkdir()
    weights = model / "model.bin"
    weights.write_bytes(b"test weights one")
    config = model / "config.json"
    config.write_text('{"layers": 1}', encoding="utf-8")
    before = model_directory_digest(model)
    assert len(before) == 64 and before == model_directory_digest(model)
    weights.write_bytes(b"test weights two")
    newer = model_directory_digest(model)
    assert newer != before
    config.write_text('{"layers": 2}', encoding="utf-8")
    assert model_directory_digest(model) not in {newer, before}
