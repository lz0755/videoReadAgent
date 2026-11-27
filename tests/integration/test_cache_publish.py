from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from video_agent.adapters.media.thin_media import LocalArtifactStore
from video_agent.adapters.storage.media_cache import FileMediaCache


def test_cache_reader_keeps_old_bytes_while_new_record_is_published(tmp_path: Path) -> None:
    root = tmp_path / "media-cache"
    cache = FileMediaCache(root, LocalArtifactStore(tmp_path / "artifacts"))
    key = "a" * 64
    cache.write(key, b"old")
    assert cache.read(key) == b"old"
    (record,) = root.iterdir()
    original = record.read_bytes()
    writing, finished = Event(), Event()

    def publish() -> None:
        writing.set()
        try:
            cache.write(key, b"new")
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with record.open("rb") as reader:
            task = pool.submit(publish)
            assert writing.wait(5)
            finished.wait(0.1)
            assert reader.read() == original
        task.result(timeout=5)
    assert cache.read(key) == b"new"
    assert list(root.iterdir()) == [record]
