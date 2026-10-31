"""Keep ``benchmarks/run.py`` from rotting: run a tiny slice of it in a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "run.py"


def test_benchmark_script_runs_quickly() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--quick", "--filter", "memoize", "--iterations", "200"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "| memoize (hit) |" in proc.stdout
    assert "| bare call |" in proc.stdout


def test_benchmark_json_output() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--quick",
            "--filter",
            "once",
            "--iterations",
            "100",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    names = [r["name"] for r in data["results"]]
    assert names == ["bare call", "once (hit)"]
    assert data["results"][1]["sync_ns"] > 0


def test_compare_script_runs_without_optional_libraries() -> None:
    script = SCRIPT.parent / "compare.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--number", "200", "--repeat", "1", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "python-wrap" in data["ns_per_call"]["cache hit"]
    assert "python-wrap" in data["ns_per_call"]["retry (success)"]


def test_soak_script_keeps_retries_bounded() -> None:
    script = SCRIPT.parent / "soak.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--duration", "1.5", "--threads", "4", "--period", "0.3",
         "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["calls"] > 0
    assert report["amplification"] <= 2.0
