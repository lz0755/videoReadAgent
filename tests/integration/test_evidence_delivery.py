import hashlib
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.published_service import client, published, sources
from video_agent.adapters.storage.connection import connect_sqlite

__all__ = ["client", "sources", "published"]
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_all_representations_share_ownership_auth_and_registered_file_checks(
    client: TestClient, published: tuple[str, str], tmp_path: Path, sources: dict[str, Path]
) -> None:
    asset, analysis = published
    root = tmp_path / "managed"
    with closing(connect_sqlite(root / "agent.sqlite3")) as conn:
        eid, pt, digest = conn.execute(
            "SELECT e.evidence_id,e.point_ms,a.sha256 FROM evidence e "
            "JOIN artifacts a USING(artifact_id) JOIN observation_evidence l USING(evidence_id) "
            "JOIN observations o USING(observation_id) "
            "WHERE o.analysis_id=? AND e.kind='frame' LIMIT 1",
            (analysis,),
        ).fetchone()
        source = (
            root
            / conn.execute(
                "SELECT relative_path FROM artifacts WHERE asset_id=? AND kind='source_video'",
                (asset,),
            ).fetchone()[0]
        )
    other = client.post(f"/v1/assets/{asset}/analyses", json={"profile": "overview"}).json()[
        "analysis_id"
    ]
    path = f"/v1/evidence/{eid}"
    params = {"analysis_id": analysis}
    meta = client.get(path, params=params).json()
    assert meta["evidence"]["actual_pts_ms"] == pt and meta["asset_id"] == asset
    frame = client.get(path, params={**params, "representation": "frame"})
    assert hashlib.sha256(frame.content).hexdigest() == digest
    video = {**params, "representation": "source_video"}
    raw = sources["F05"].read_bytes()
    full = client.get(path, params=video)
    assert full.status_code == 200 and full.content == raw
    part = client.get(path, params=video, headers={"Range": "bytes=17-43"})
    assert part.status_code == 206 and part.content == raw[17:44]
    assert part.headers["content-range"] == f"bytes 17-43/{len(raw)}"
    past = client.get(path, params=video, headers={"Range": f"bytes={len(raw)}-"})
    assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{len(raw)}"
    for representation in ("meta", "frame", "source_video"):
        assert (
            client.get(
                path, params={"analysis_id": other, "representation": representation}
            ).status_code
            == 404
        )
        assert (
            client.get(
                path,
                params={**params, "representation": representation},
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
    source.unlink()
    gone = client.get(path, params=video)
    assert gone.status_code == 404 and str(root) not in gone.text
