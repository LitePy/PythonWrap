"""Static typing checks, run through mypy in a subprocess."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")

CASES = Path(__file__).parent / "typing_cases" / "suppress_cases.py"


def test_suppress_with_exception_types_is_typed_as_a_decorator() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(CASES)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout
    assert "error:" not in out, out
    assert out.count("-> int | None") == 3, out
    assert "Coroutine[Any, Any, int | None]" in out, out
