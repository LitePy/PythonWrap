"""Build the wheel, install it into a fresh venv with no dev dependencies, and smoke test it.

Usage: python scripts/smoke_wheel.py [--python PYTHON]

Steps: build one wheel from the checkout (``uv build`` if available, else ``python -m build``),
create an empty virtual environment, install only that wheel, then run a short script with
``python -I`` from a scratch directory so the source tree cannot stand in for anything the
wheel left out. The script checks the version is a PEP 440 beta, that there are no runtime
dependencies, that ``py.typed`` and the packaged data files ship, that the command line tools
work and that a retry, timeout and LLM happy path runs. Standard library only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECK = textwrap.dedent(
    """
    import importlib.metadata as md
    import json
    import re
    import subprocess
    import sys
    from pathlib import Path

    import python_wrap

    version = python_wrap.__version__
    assert re.fullmatch(r"\\d+\\.\\d+\\.\\d+b\\d+", version), f"not a PEP 440 beta: {version}"
    meta = md.metadata("python-wrap")
    assert meta["Version"] == version
    requires = [r for r in (md.requires("python-wrap") or []) if "extra ==" not in r]
    assert requires == [], f"runtime dependencies: {requires}"

    package = Path(python_wrap.__file__).parent
    assert (package / "py.typed").is_file(), "py.typed missing"
    for name in ("agent_guide.md", "recipes.json"):
        assert (package / "_data" / name).is_file(), name

    work = Path(sys.argv[1])

    def cli(*args):
        done = subprocess.run(
            [sys.executable, "-I", "-m", "python_wrap", *args],
            capture_output=True, text=True, timeout=60, cwd=work,
        )
        assert done.returncode == 0, (args, done.stderr)
        return done.stdout

    assert "python-wrap" in cli("guide").lower()
    assert json.loads(cli("api", "--json"))
    assert json.loads(cli("recipe", "retry", "http", "calls", "--json"))
    (work / "stacks.py").write_text(
        "from python_wrap import policy, retry, timeout\\n"
        "ok = policy(name='ok', retry=retry(max_attempts=3, on=ConnectionError),"
        " timeout=timeout(2))\\n"
    )
    (work / "legacy.py").write_text(
        "from tenacity import retry, stop_after_attempt\\n\\n\\n"
        "@retry(stop=stop_after_attempt(3))\\ndef f():\\n    return 1\\n"
    )
    assert "retry" in cli("doctor", "stacks:ok")
    assert "max_attempts=3" in cli("migrate", "--diff", str(work / "legacy.py"))

    from python_wrap import retry, timeout
    from python_wrap.llm import Budget
    from python_wrap.presets import llm_client
    from python_wrap import fake_llm
    from python_wrap.testing import fake_clock
    from python_wrap.llm_testing import RateLimitError

    calls = []

    @retry(max_attempts=3, delay=1, on=ConnectionError)
    @timeout(5)
    def fetch():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return "ok"

    provider = fake_llm([RateLimitError("slow", status_code=429, retry_after=2.0), "hi"])
    spend = Budget(tokens=1000, name="smoke")

    @llm_client("smoke", spend=spend)
    def ask(question):
        return provider.complete(messages=[{"role": "user", "content": question}])

    with fake_clock() as clock:
        assert fetch() == "ok" and len(calls) == 3
        assert ask("hello").text == "hi" and provider.call_count == 2
        assert clock.sleeps[-1] == 2.0
    print(f"smoke ok: python-wrap {version}")
    """
)


def run(command: list[str], **kwargs: object) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, **kwargs)  # type: ignore[call-overload]


def build_wheel(out: Path) -> Path:
    if shutil.which("uv"):
        run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT)
    else:
        run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out)], cwd=ROOT)
    wheels = sorted(out.glob("python_wrap-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {out}, found {wheels}")
    return wheels[0]


def make_venv(venv: Path, python: str) -> Path:
    if shutil.which("uv"):
        run(["uv", "venv", "--python", python, str(venv)])
    else:
        run([python, "-m", "venv", str(venv)])
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return venv / scripts / ("python.exe" if sys.platform == "win32" else "python")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--python", default=sys.executable, help="interpreter for the new venv")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pw-smoke-") as tmp:
        base = Path(tmp)
        wheel = build_wheel(base / "dist")
        interpreter = make_venv(base / "venv", args.python)
        if shutil.which("uv"):
            run(["uv", "pip", "install", "--python", str(interpreter), str(wheel)])
        else:
            run([str(interpreter), "-m", "pip", "install", "--no-input", str(wheel)])
        work = base / "work"
        work.mkdir()
        script = work / "check.py"
        script.write_text(CHECK)
        run([str(interpreter), "-I", str(script), str(work)], cwd=work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
