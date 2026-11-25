"""OS-level network isolation canary for --network none CI containers.

Probes a public IP literal (1.1.1.1:443) from both an independent isolated Python
subprocess (-I) and a non-Python native subprocess (curl) to avoid relying only
on DNS failure or pytest-socket inside the main pytest process.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

PROBE_CODE = """
import errno
import socket
import sys

try:
    with socket.create_connection(("1.1.1.1", 443), timeout=2):
        sys.exit(0)
except OSError as exc:
    expected = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM}
    sys.exit(42 if exc.errno in expected else 43)
"""


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE_CODE],
        check=False,
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 42:
        raise SystemExit(f"network isolation not proven: exit={result.returncode}")

    curl = shutil.which("curl")
    if curl is None:
        raise SystemExit("curl is missing; non-Python isolation was not tested")

    native = subprocess.run(
        [
            curl,
            "--disable",
            "--proxy",
            "",
            "--noproxy",
            "*",
            "--verbose",
            "--connect-timeout",
            "2",
            "--max-time",
            "4",
            "https://1.1.1.1:443/",
        ],
        env={"LC_ALL": "C"},
        check=False,
        capture_output=True,
        text=True,
        timeout=6,
    )
    blocked_reasons = (
        "Network is unreachable",
        "No route to host",
        "Permission denied",
        "Operation not permitted",
    )
    if native.returncode != 7 or not any(reason in native.stderr for reason in blocked_reasons):
        raise SystemExit(f"non-Python isolation not proven: curl exit={native.returncode}")

    print("canary passed: independent Python and curl were blocked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
