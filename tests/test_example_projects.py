"""Each project in ``examples/projects/`` must run its own tests and its ``main.py`` or ``run.py``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = sorted(
    path.parent for path in (ROOT / "examples" / "projects").glob("*/tests") if path.is_dir()
)


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
    }


def test_projects_exist() -> None:
    assert len(PROJECTS) >= 2


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_project_tests_pass(project: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(project / "tests"), "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_env(),
        cwd=project,
    )
    assert result.returncode == 0, f"{project.name} tests failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_project_main_runs(project: Path) -> None:
    entry = next(p for p in (project / "main.py", project / "run.py") if p.exists())
    result = subprocess.run(
        [sys.executable, str(entry)],
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(),
        cwd=ROOT,
    )
    assert result.returncode == 0, f"{project.name} failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip()
