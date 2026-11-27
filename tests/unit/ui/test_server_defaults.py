import json
import os
import subprocess
import sys
from pathlib import Path


def test_streamlit_loads_local_review_defaults(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[3]
    options = ["server.address", "server.maxUploadSize", "browser.gatherUsageStats"]
    probe = (
        "import json,sys; from streamlit import config; "
        "values = [[config.get_option(k),config.get_where_defined(k)] for k in sys.argv[1:]]; "
        "print(json.dumps(values))"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("STREAMLIT_")}
    result = subprocess.run(
        [sys.executable, "-c", probe, *options],
        cwd=project,
        env={**clean, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    settings = json.loads(result.stdout)
    assert [value for value, _ in settings] == ["127.0.0.1", 64, False]
    expected = project / ".streamlit/config.toml"
    assert all(Path(source).resolve() == expected for _, source in settings)
