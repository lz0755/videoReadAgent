import hashlib
from pathlib import Path

import pytest

from eval import report


def test_review_progress_can_finish_on_the_same_sealed_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = {
        "reviews_sha256": "a" * 64,
        "quality_passed": False,
        "metrics": {},
        "pending_reviews": ["q"],
        "second_reviews": {"independent": 0, "delayed_self": 0, "population": 32},
    }
    complete = pending | {
        "reviews_sha256": "b" * 64,
        "quality_passed": True,
        "pending_reviews": [],
        "second_reviews": {"independent": 7, "delayed_self": 0, "population": 32},
    }
    grades = iter([pending, complete, complete, complete | {"quality_passed": False}])
    monkeypatch.setattr(report, "adjudicate", lambda *_: next(grades))
    args = ["--run-dir", str(tmp_path), "--packet", "private", "--reviews", "review"]
    assert report.main(args) == 1
    target = tmp_path / "quality.json"
    assert not target.exists()
    assert len(list(tmp_path.glob("quality-review-*.json"))) == 1
    assert report.main(args) == 0
    original = hashlib.sha256(target.read_bytes()).digest()
    assert report.main(args) == 0
    assert report.main(args) == 2
    assert hashlib.sha256(target.read_bytes()).digest() == original
