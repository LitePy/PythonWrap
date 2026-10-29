"""Check an installed distribution: python -I scripts/check_installed.py VERSION.

Run with an isolated interpreter in a clean environment so the checkout cannot
mask missing wheel files. Uses only the standard library and python-wrap.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import inspect
import pkgutil
import sys
from pathlib import Path

import python_wrap
from python_wrap import memoize, retry


def main() -> None:
    expected = sys.argv[1]
    metadata = importlib.metadata.metadata("python-wrap")
    assert metadata["Version"] == python_wrap.__version__ == expected
    assert not metadata.get_all("Requires-Dist"), "Unexpected runtime dependencies"
    assert metadata["Requires-Python"] == ">=3.10"
    assert metadata["License-Expression"] == "MIT"
    assert (Path(python_wrap.__file__).parent / "py.typed").is_file()
    for module in pkgutil.iter_modules(python_wrap.__path__):
        importlib.import_module(f"python_wrap.{module.name}")
    for name in python_wrap.__all__:
        assert hasattr(python_wrap, name), name

    attempts = 0

    @retry(max_attempts=2, delay=0)
    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("temporary")
        return "ok"

    assert flaky() == "ok" and attempts == 2
    calls = 0

    @memoize
    def cached(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    assert cached(3) == cached(3) == 6 and calls == 1

    @retry(max_attempts=2, delay=0)
    async def asynchronous(value: int) -> int:
        return value + 1

    assert inspect.iscoroutinefunction(asynchronous)
    assert asyncio.run(asynchronous(2)) == 3
    print(f"python-wrap {expected}: metadata, imports, typing marker, sync and async checks passed")


if __name__ == "__main__":
    main()
