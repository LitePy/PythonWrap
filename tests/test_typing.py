"""Static typing checks, run through mypy in a subprocess.

The cases live in ``tests/typing_cases/`` and are type-checked, never imported. mypy runs
once for all of them (``--strict`` includes ``--warn-unused-ignores``, so a deliberate
mistake that stops being flagged fails the run).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")

CASES_DIR = Path(__file__).parent / "typing_cases"
SUPPRESS_CASES = CASES_DIR / "suppress_cases.py"
DECORATOR_CASES = CASES_DIR / "decorator_cases.py"


@pytest.fixture(scope="module")
def mypy_output() -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--show-error-codes",
            str(SUPPRESS_CASES),
            str(DECORATOR_CASES),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout + proc.stderr


def _lines_for(output: str, path: Path) -> list[str]:
    # mypy may print the path relative to the working directory.
    tail = f"{path.parent.name}/{path.name}:"
    return [line for line in output.splitlines() if tail in line.replace("\\", "/")]


def test_suppress_with_exception_types_is_typed_as_a_decorator(mypy_output: str) -> None:
    out = "\n".join(_lines_for(mypy_output, SUPPRESS_CASES))
    assert "error:" not in out, out
    assert out.count("-> int | None") == 3, out
    assert "Coroutine[Any, Any, int | None]" in out, out


def test_decorators_keep_types_bind_methods_and_expose_attributes(mypy_output: str) -> None:
    # Stacking keeps inner attributes, methods/classmethods bind, `.breaker` is typed, and
    # every deliberate mistake is still reported (else: unused "type: ignore").
    out = "\n".join(_lines_for(mypy_output, DECORATOR_CASES))
    assert "error:" not in out, out
    assert "no issues found in 2 source files" in mypy_output, mypy_output
