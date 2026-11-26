"""M2 entry-point regressions for catalog integrity and current import limits."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from tests.fixtures.media_factory import generate_f01_cfr, self_check_f01_cfr
from video_agent.bootstrap import SliceContainer, build_slice_container
from video_agent.domain.errors import ErrorCode, VideoAgentError
from video_agent.domain.models import TimeRangeMs, VideoAsset


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    movie = tmp_path_factory.mktemp("cached-source") / "fixture.mp4"
    generate_f01_cfr(movie)
    assert self_check_f01_cfr(movie).passed
    return movie


def _produce(container: SliceContainer, asset: VideoAsset, kind: str) -> str:
    backend = container.media_backend
    if kind == "audio":
        return backend.extract_audio(asset)[0]
    (frame,) = backend.extract_frames(asset, [40], range_ms=TimeRangeMs(start_ms=0, end_ms=200))
    return frame.artifact_id


@pytest.mark.parametrize(
    ("kind", "catalog_state"),
    [
        ("frame", "missing"),
        ("audio", "missing"),
        ("frame", "wrong_kind"),
        ("frame", "wrong_owner"),
        ("audio", "wrong_path"),
    ],
)
def test_warm_cache_recreates_owned_catalog_reference(
    tmp_path: Path, source: Path, kind: str, catalog_state: str
) -> None:
    root = tmp_path / "data"
    with closing(build_slice_container(mode="offline", data_root=root)) as app:
        asset = app.media_backend.probe_video(str(source), asset_id="registered")
        other = app.media_backend.probe_video(str(source), asset_id="other")
        old = _produce(app, asset, kind)

    with closing(sqlite3.connect(root / "agent.sqlite3")) as db:
        path, sha = db.execute(
            "SELECT relative_path,sha256 FROM artifacts "
            "WHERE artifact_id=? AND asset_id=? AND kind=?",
            (old, asset.asset_id, kind),
        ).fetchone()
        assert (root / path).is_file()
        if catalog_state == "missing":
            db.execute("DELETE FROM artifacts WHERE artifact_id=?", (old,))
        elif catalog_state == "wrong_kind":
            db.execute("UPDATE artifacts SET kind='export' WHERE artifact_id=?", (old,))
        elif catalog_state == "wrong_owner":
            db.execute("UPDATE artifacts SET asset_id=? WHERE artifact_id=?", (other.asset_id, old))
        else:
            db.execute(
                "UPDATE artifacts SET relative_path=? WHERE artifact_id=?",
                ("artifacts/registered/missing.wav", old),
            )
        db.commit()

    with closing(build_slice_container(mode="offline", data_root=root)) as app:
        registered = app.media_backend.probe_video(str(source), asset_id=asset.asset_id)
        actual = _produce(app, registered, kind)
    with closing(sqlite3.connect(root / "agent.sqlite3")) as db:
        row = db.execute(
            "SELECT relative_path,sha256 FROM artifacts "
            "WHERE artifact_id=? AND asset_id=? AND kind=?",
            (actual, asset.asset_id, kind),
        ).fetchone()
    assert row is not None, "returned evidence must reference an owned catalog row"
    assert (root / row[0]).is_file() and row[1] == sha
    with (root / row[0]).open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == sha


@pytest.mark.parametrize("limit", ["max_duration_ms", "max_file_bytes"])
def test_reopened_fixed_asset_obeys_current_limits(
    tmp_path: Path, source: Path, limit: str
) -> None:
    root = tmp_path / "data"
    with closing(build_slice_container(mode="offline", data_root=root)) as app:
        asset = app.media_backend.probe_video(str(source), asset_id="fixed")
    settings = yaml.safe_load(Path("config/defaults.yaml").read_text(encoding="utf-8"))
    previous = asset.duration_ms if limit == "max_duration_ms" else asset.size_bytes
    settings["media"][limit] = previous - 1
    config_path = tmp_path / "stricter.yaml"
    config_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with closing(
        build_slice_container(mode="offline", data_root=root, config_path=config_path)
    ) as app:
        for asset_id in (asset.asset_id, "new"):
            with pytest.raises(VideoAgentError) as exc:
                app.media_backend.probe_video(str(source), asset_id=asset_id)
            assert exc.value.code is ErrorCode.MEDIA_LIMIT_EXCEEDED
    with closing(sqlite3.connect(root / "agent.sqlite3")) as db:
        assert db.execute("SELECT asset_id,status,sha256 FROM assets").fetchall() == [
            (asset.asset_id, "ready", asset.sha256)
        ]


def test_distinct_valid_asset_ids_keep_literal_owned_artifacts(
    tmp_path: Path, source: Path
) -> None:
    root = tmp_path / "data"
    owners = ("a", "_a_")
    with closing(build_slice_container(mode="offline", data_root=root)) as app:
        for owner in owners:
            asset = app.media_backend.probe_video(str(source), asset_id=owner)
            assert asset.asset_id == owner
            for kind in ("audio", "frame"):
                assert _produce(app, asset, kind)
    with closing(sqlite3.connect(root / "agent.sqlite3")) as db:
        rows = db.execute(
            "SELECT artifact_id,asset_id,kind,relative_path FROM artifacts WHERE kind!='export'"
        ).fetchall()
    assert {(o, k) for _, o, k, _ in rows} == {
        (o, k) for o in owners for k in ("source_video", "frame", "audio")
    }
    assert len({artifact_id for artifact_id, _, _, _ in rows}) == 6
    for _, owner, _, relative in rows:
        assert Path(relative).parent.name == owner
        assert (root / relative).is_file()
