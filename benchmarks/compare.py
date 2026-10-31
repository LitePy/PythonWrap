"""Compare per-call overhead with the specialist libraries (tenacity, cachetools, ...).

Measures the same trivial operations wrapped by python-wrap and, when installed, by
``tenacity`` (retry on a function that succeeds first time), ``backoff``, ``cachetools``
(cache hit) and ``functools.lru_cache`` (always available). Libraries that are not
installed are skipped, so the script never fails for a missing optional package.

    python benchmarks/compare.py                    # human-readable table
    python benchmarks/compare.py --json             # machine-readable
    uv run --with tenacity --with cachetools --with backoff python benchmarks/compare.py

With ``--max-ratio X`` it exits 1 when a python-wrap decorator is more than ``X`` times
slower than the fastest pure-Python specialist for the same case: CI uses that as a coarse
regression gate (timings on shared runners are noisy, so keep ``X`` generous).
"""

from __future__ import annotations

import argparse
import functools
import importlib
import json
import sys
import timeit
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import python_wrap  # noqa: F401
except ImportError:  # running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from python_wrap import memoize, retry


def _optional(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _add(a: int, b: int) -> int:
    return a + b


def cases() -> dict[str, dict[str, Callable[[], Any]]]:
    """case -> implementation name -> zero-argument callable to time."""
    result: dict[str, dict[str, Callable[[], Any]]] = {"retry (success)": {}, "cache hit": {}}
    retried = retry(max_attempts=3, delay=0)(_add)
    result["retry (success)"]["python-wrap"] = lambda: retried(1, 2)
    tenacity = _optional("tenacity")
    if tenacity is not None:
        t = tenacity.retry(stop=tenacity.stop_after_attempt(3), reraise=True)(_add)
        result["retry (success)"]["tenacity"] = lambda: t(1, 2)
    backoff = _optional("backoff")
    if backoff is not None:
        b = backoff.on_exception(backoff.expo, Exception, max_tries=3)(_add)
        result["retry (success)"]["backoff"] = lambda: b(1, 2)

    cached = memoize(_add)
    result["cache hit"]["python-wrap"] = lambda: cached(1, 2)
    lru = functools.lru_cache(maxsize=128)(_add)
    result["cache hit"]["functools.lru_cache"] = lambda: lru(1, 2)
    cachetools = _optional("cachetools")
    if cachetools is not None:
        c = cachetools.cached(cachetools.LRUCache(maxsize=128))(_add)
        result["cache hit"]["cachetools"] = lambda: c(1, 2)
        ttl = cachetools.cached(cachetools.TTLCache(maxsize=128, ttl=60))(_add)
        result["cache hit"]["cachetools (TTL)"] = lambda: ttl(1, 2)
    return result


def measure(func: Callable[[], Any], number: int, repeat: int) -> float:
    """Best-of-``repeat`` nanoseconds per call."""
    func()  # warm up (fills caches)
    return min(timeit.repeat(func, number=number, repeat=repeat)) / number * 1e9


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="python-wrap vs specialist libraries")
    parser.add_argument("--number", type=int, default=20_000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-ratio", type=float, default=None)
    args = parser.parse_args(argv)

    report: dict[str, dict[str, float]] = {}
    for case, impls in cases().items():
        report[case] = {name: measure(f, args.number, args.repeat) for name, f in impls.items()}

    failed = []
    for case, timings in report.items():
        # lru_cache is implemented in C and has no TTL; it is shown for scale only.
        others = [
            ns for name, ns in timings.items() if name not in {"python-wrap", "functools.lru_cache"}
        ]
        too_slow = args.max_ratio is not None and others
        if too_slow and timings["python-wrap"] > args.max_ratio * min(others):
            failed.append(case)

    if args.json:
        print(json.dumps({"ns_per_call": report, "over_ratio": failed}, indent=2))
    else:
        print("| case | implementation | ns/call |")
        print("| --- | --- | ---: |")
        for case, timings in report.items():
            for name, ns in sorted(timings.items(), key=lambda item: item[1]):
                print(f"| {case} | {name} | {ns:,.0f} |")
        if failed:
            print(f"\nslower than {args.max_ratio}x the fastest alternative: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
