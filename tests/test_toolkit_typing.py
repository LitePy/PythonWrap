import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")


def test_new_public_apis_preserve_types():
    path = Path(__file__).parent / "typing_cases" / "toolkit_cases.py"
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
