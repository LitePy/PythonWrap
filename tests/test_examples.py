"""Every script in ``examples/`` must run to completion with exit code 0."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))


def test_examples_exist() -> None:
    assert len(EXAMPLES) >= 5


@pytest.mark.parametrize("script", EXAMPLES, ids=lambda p: p.name)
def test_example_runs(script: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"{script.name} failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip(), f"{script.name} printed nothing"
