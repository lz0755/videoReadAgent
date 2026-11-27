"""Real process failure points in the regular CLI. No storage or subprocess substitutes."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

from video_agent.bootstrap import build_jobs, load_app_config
from video_agent.cli import main
from video_agent.domain.models import Job
from video_agent.worker import run_queue


def main_fault(root: Path, fault: str) -> int:
    if fault == "child":
        (root / "child-ready").touch()
        for _ in range(400):
            if (root / "stop-child").exists():
                break
            time.sleep(0.05)
        (root / "child-done").touch()
        return 0

    if fault == "orphan":

        def compute(job: Job) -> None:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    __name__ if __name__ != "__main__" else "tests.fixtures.worker_faults",
                    str(root),
                    "child",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            for _ in range(100):
                if (root / "child-ready").exists():
                    os._exit(86)
                time.sleep(0.05)
            raise RuntimeError("compute child failed to start")

        run_queue(
            root,
            build_jobs(root, load_app_config()),
            compute,
            analysis_seconds=120,
            qa_seconds=60,
            once=True,
        )
        return 1

    targets = {
        "before-stage": ("video_agent.adapters.storage.observations", "write_window"),
        "after-stage": ("video_agent.adapters.storage.search_index", "SQLiteSearchIndex.publish"),
        "cancel": ("video_agent.adapters.storage.stages", "StageCache.commit"),
        "pending": ("video_agent.adapters.llm.fake", "FakePlannerProvider.decide"),
    }

    def checkpoint(frame: FrameType, event: str, arg: object) -> None:
        if (
            event != "call"
            or (frame.f_globals.get("__name__"), frame.f_code.co_qualname) != targets[fault]
        ):
            return
        if fault == "cancel":
            (root / "at-stage").touch()
            for _ in range(240):
                if (root / "resume-stage").exists():
                    return
                time.sleep(0.05)
            raise RuntimeError("cancel checkpoint expired")
        os._exit(86)

    sys.setprofile(checkpoint)
    try:
        return main(["worker", "--offline", "--once", "--data-root", str(root)])
    finally:
        sys.setprofile(None)


if __name__ == "__main__":
    raise SystemExit(main_fault(Path(sys.argv[1]), sys.argv[2]))
