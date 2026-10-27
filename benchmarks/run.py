"""Micro-benchmarks for python-wrap decorators (standard library only).

For every decorator this measures the per-call cost of a trivial function wrapped with
it, for sync and ``async def`` functions, next to the bare (undecorated) call.

    python benchmarks/run.py                 # full run (about a minute)
    python benchmarks/run.py --quick         # <= 5 s, noisier
    python benchmarks/run.py --filter memoize --json

Numbers are total nanoseconds per call (loop overhead included, identical for the bare
baseline), so ``x bare`` is the slowdown factor. Timings are the best of several repeats.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import platform
import signal
import sys
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import python_wrap  # noqa: F401
except ImportError:  # running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from python_wrap.batching import batch_calls
from python_wrap.bulkhead import Bulkhead, limit_concurrency
from python_wrap.caching import cache, memoize, once
from python_wrap.circuit import circuit_breaker
from python_wrap.concurrency import rate_limit, singleflight, throttle, timeout
from python_wrap.devtools import deprecate, mock_data
from python_wrap.logs import audit, log_args
from python_wrap.retry import retry
from python_wrap.timing import timing
from python_wrap.validation import check_type, enforce_types, validate_args

perf_ns = time.perf_counter_ns

Fn = Callable[..., Any]
Factory = Callable[[Fn], Fn]


def add(a: int, b: int) -> int:
    return a + b


async def aadd(a: int, b: int) -> int:
    return a + b


@dataclass
class Case:
    name: str
    sync: Factory | None  # takes the bare sync function, returns the decorated one
    asyn: Factory | None  # same for the async function


def _quiet_logger() -> logging.Logger:
    lg = logging.getLogger("python_wrap.bench")
    lg.setLevel(logging.CRITICAL + 10)
    lg.propagate = False
    return lg


LOG = _quiet_logger()
HAS_SIGNALS = hasattr(signal, "setitimer") and sys.platform != "win32"


def _both(make: Callable[[], Callable[[Any], Any]]) -> Case:
    """A case whose decorator works on sync and async functions alike."""
    return Case("", lambda fn: make()(fn), lambda fn: make()(fn))


def _case(name: str, make: Callable[[], Callable[[Any], Any]]) -> Case:
    c = _both(make)
    c.name = name
    return c


def _stack(fn: Fn) -> Fn:
    return retry(max_attempts=3, delay=0, logger=LOG)(
        timeout(5, use_signals=False)(circuit_breaker(failure_threshold=10, logger=LOG)(fn))
    )


def _stack_async(fn: Fn) -> Fn:
    return retry(max_attempts=3, delay=0, logger=LOG)(
        timeout(5)(circuit_breaker(failure_threshold=10, logger=LOG)(fn))
    )


def _batch_async(fn: Fn) -> Fn:
    @batch_calls(max_size=1, max_wait=0)
    async def batched(items: list[int]) -> list[int]:
        return items

    async def call(a: int, b: int) -> int:
        return int(await batched(a))

    return call


def cases() -> list[Case]:
    out = [
        _case("timing (logging off)", lambda: timing(logger=LOG)),
        _case("retry (success path)", lambda: retry(max_attempts=3, delay=0, logger=LOG)),
        _case("memoize (hit)", lambda: memoize),
        _case("memoize(maxsize=128) (hit)", lambda: memoize(maxsize=128)),
        Case("functools.lru_cache(128) (hit)", functools.lru_cache(maxsize=128), None),
        Case("functools.cache (hit)", functools.cache, None),
        _case("once (hit)", lambda: once),
        _case("cache(60) (hit)", lambda: cache(60)),
        _case("rate_limit (under limit)", lambda: rate_limit(10**9, 1.0)),
        _case("throttle (skipped)", lambda: throttle(3600)),
        _case("singleflight (uncontended)", lambda: singleflight),
        _case("circuit_breaker (closed)", lambda: circuit_breaker(logger=LOG)),
        Case("timeout (thread fallback)", timeout(5, use_signals=False), None),
        Case("timeout (signal)", timeout(5, use_signals=True), None) if HAS_SIGNALS else None,
        Case("timeout (async)", None, timeout(5)),
        _case("log_args (level off)", lambda: log_args(logger=LOG)),
        _case("audit (logging off)", lambda: audit(logger=LOG)),
        _case("validate_args", lambda: validate_args(a=lambda v: v >= 0, b=lambda v: v >= 0)),
        _case("check_type", lambda: check_type(a=int, b=int)),
        _case("enforce_types", lambda: enforce_types),
        _case("deprecate (warning ignored)", lambda: deprecate("old")),
        _case("mock_data (disabled)", lambda: mock_data(1, enabled=False)),
        _case("mock_data (enabled)", lambda: mock_data(1)),
        _case("limit_concurrency(8)", lambda: limit_concurrency(8)),
        _case("Bulkhead(8)", lambda: Bulkhead(8)),
        Case("batch_calls (max_size=1)", None, _batch_async),
        Case("retry+timeout+circuit_breaker", _stack, _stack_async),
    ]
    return [c for c in out if c is not None]


# -- measuring ------------------------------------------------------------------------


def _time_sync(f: Fn, n: int) -> float:
    t0 = perf_ns()
    for _ in range(n):
        f(1, 2)
    return (perf_ns() - t0) / n


async def _arun(f: Callable[..., Awaitable[Any]], n: int) -> float:
    t0 = perf_ns()
    for _ in range(n):
        await f(1, 2)
    return (perf_ns() - t0) / n


def _time_async(f: Fn, n: int) -> float:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_arun(f, n))
    finally:
        loop.close()


def _measure(
    timer: Callable[[Fn, int], float], f: Fn, budget_ns: float, repeats: int, n_fixed: int | None
) -> float:
    for _ in range(3):  # warm-up: fills caches, runs first-call setup
        timer(f, 1)
    if n_fixed:
        n = n_fixed
    else:
        probe = max(timer(f, 20), 1.0)
        n = max(20, min(200_000, int(budget_ns / repeats / probe)))
    return min(timer(f, n) for _ in range(repeats))


def run(selected: list[Case], quick: bool, iterations: int | None) -> list[dict[str, Any]]:
    cells = sum((c.sync is not None) + (c.asyn is not None) for c in selected) + 2
    budget = (2.5 / cells if quick else 0.35) * 1e9  # ns of measuring per cell
    repeats = 3 if quick else 7
    base = {
        "sync": _measure(_time_sync, add, budget, repeats, iterations),
        "async": _measure(_time_async, aadd, budget, repeats, iterations),
    }
    results: list[dict[str, Any]] = [
        {
            "name": "bare call",
            "sync_ns": base["sync"],
            "async_ns": base["async"],
            "sync_x": 1.0,
            "async_x": 1.0,
        }
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for c in selected:
            row: dict[str, Any] = {
                "name": c.name,
                "sync_ns": None,
                "async_ns": None,
                "sync_x": None,
                "async_x": None,
            }
            for kind, factory, fn, timer in (
                ("sync", c.sync, add, _time_sync),
                ("async", c.asyn, aadd, _time_async),
            ):
                if factory is None:
                    continue
                try:
                    ns = _measure(timer, factory(fn), budget, repeats, iterations)
                except Exception as exc:  # report, don't abort the whole run
                    row[f"{kind}_error"] = f"{type(exc).__name__}: {exc}"
                    continue
                row[f"{kind}_ns"] = ns
                row[f"{kind}_x"] = ns / base[kind]
            results.append(row)
    return results


def _fmt(ns: float | None) -> str:
    return "-" if ns is None else f"{ns:,.0f}"


def _fx(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}x"


def markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Decorator | sync ns/call | async ns/call | sync x bare | async x bare |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | {_fmt(r['sync_ns'])} | {_fmt(r['async_ns'])} "
            f"| {_fx(r['sync_x'])} | {_fx(r['async_x'])} |"
        )
    for r in results:
        for k in ("sync_error", "async_error"):
            if k in r:
                lines.append(f"\n> {r['name']} ({k[:-6]}): {r[k]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--quick", action="store_true", help="finish in <= 5 s (noisier numbers)")
    p.add_argument("--filter", default="", help="only decorators whose name contains this text")
    p.add_argument("--json", action="store_true", help="print JSON instead of a Markdown table")
    p.add_argument(
        "--iterations", type=int, default=None, help="fixed calls per repeat (skip calibration)"
    )
    args = p.parse_args(argv)

    selected = [c for c in cases() if args.filter.lower() in c.name.lower()]
    if not selected:
        print(f"no decorator matches {args.filter!r}", file=sys.stderr)
        return 2
    results = run(selected, args.quick, args.iterations)
    if args.json:
        meta = {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "quick": args.quick,
        }
        print(json.dumps({"meta": meta, "results": results}, indent=2))
    else:
        print(markdown(results))
        print(f"\nPython {platform.python_version()} on {platform.platform()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
