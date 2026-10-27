"""Decorators for command-line tools and scripts.

Time a step and collect the numbers, profile a hot function, benchmark two
implementations, call async code from a sync ``main`` with ``run_sync``, rebuild once
after a burst of file-change events with ``debounce`` and keep a progress line from
flooding the terminal with ``throttle``.

Run: python examples/cli_tools.py
"""

from __future__ import annotations

import asyncio
import pstats
import time

from python_wrap import (
    BenchmarkResult,
    TimingRecord,
    benchmark,
    debounce,
    profile,
    throttle,
    timing,
)
from python_wrap.aio import run_sync

# 1. Time a step. The callback receives a record for every call (feed a metrics system).
timings: list[TimingRecord] = []


@timing(callback=timings.append, threshold=10.0)  # only *log* calls slower than 10 s
def load_data() -> list[int]:
    return list(range(50_000))


# 2. Profile a function and keep the pstats object instead of printing.
profiles: list[pstats.Stats] = []


@profile(sort="tottime", limit=5, callback=profiles.append)
def crunch(data: list[int]) -> int:
    return sum(x * x for x in data)


# 3. Async code behind a sync CLI entry point.
async def fetch_remote_total() -> int:
    await asyncio.sleep(0.001)
    return 7


# 4. File watcher: a save usually fires several events; rebuild once, after they stop.
builds: list[str] = []


@debounce(0.03)
def rebuild(path: str) -> None:
    builds.append(path)


# 5. Progress output at most once per interval; skipped calls return the last result.
printed: list[int] = []


@throttle(0.05)
def show_progress(done: int) -> None:
    printed.append(done)


# --- demo ---


def main() -> None:
    data = load_data()
    print(f"load_data took {timings[0].elapsed * 1000:.1f} ms")
    assert timings[0].success and timings[0].name.endswith("load_data")

    assert crunch(data) == sum(x * x for x in data)
    assert isinstance(profiles[0], pstats.Stats)

    # Compare two implementations with real statistics.
    def slow() -> int:
        return sum(list(range(2_000)))

    def fast() -> int:
        return sum(range(2_000))

    results: list[BenchmarkResult] = [benchmark(f, runs=20, warmup=3) for f in (slow, fast)]
    for r in results:
        print(f"{r.name}: median {r.median * 1e6:.1f} us over {r.runs} runs")
    assert all(r.min <= r.median <= r.max for r in results)

    assert run_sync(fetch_remote_total) == 7
    print("run_sync returned 7")

    for _ in range(5):  # five events from one save
        rebuild("src/app.py")
    assert builds == []
    time.sleep(0.08)  # the quiet period; a real watcher just keeps running
    assert builds == ["src/app.py"]
    print("5 file events -> 1 rebuild")

    rebuild("final.py")
    rebuild.flush()  # at shutdown: run the pending call now instead of losing it
    assert builds[-1] == "final.py"

    for done in range(100):
        show_progress(done)
    assert printed == [0]
    print("100 progress updates -> printed", printed)


if __name__ == "__main__":
    main()
