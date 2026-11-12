"""Wheel smoke test: builds the wheel and installs it into a clean venv (opt-in)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(os.environ.get("PW_SMOKE_WHEEL") != "1", reason="set PW_SMOKE_WHEEL=1 to run")
def test_built_wheel_installs_and_works_in_a_clean_venv() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_wheel.py")],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "smoke ok" in result.stdout
