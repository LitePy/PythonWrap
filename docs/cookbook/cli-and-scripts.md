# CLI tools and scripts

## The problem

Scripts and command-line tools have their own small recurring needs:

- "How long did each step take?" without sprinkling `time.time()` around.
- "Why is this slow?" without learning `cProfile`'s API.
- "Is version B really faster than version A?" with more than one measurement.
- Calling `async` library code from a normal `main()`.
- A **file watcher** that fires five events for one save and rebuilds five times.
- A progress line that prints ten thousand times a second.

## The code

```python
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


main()
```

## How it works

* **`@timing(callback=..., threshold=...)`** measures every call with `perf_counter` (for `async def` it times the whole awaited coroutine) and logs `name took 12.34 ms`. `callback` receives a `TimingRecord(name, elapsed, success)` for **every** call, which is how you feed a metrics system or build a summary table at the end of the run. `threshold` only suppresses the *log line* for fast successful calls.
* **`@profile(callback=...)`** runs the function under `cProfile` and hands you the `pstats.Stats`. Without a callback it logs the top `limit` rows sorted by `sort`.
* **`benchmark(func, args=..., runs=..., warmup=...)`** is a plain function (not a decorator). It calls the function `warmup` times, then `runs` times, and returns a `BenchmarkResult` with `mean`, `median`, `min`, `max` and `stdev` in seconds. Compare **medians**; the mean is dragged around by outliers. Use `benchmark_async` for coroutine functions.
* **`run_sync(coro_or_function, *args)`** is `asyncio.run` for a sync entry point. It accepts a coroutine or a function plus arguments.
* **`@debounce(wait)`** postpones the call until `wait` seconds have passed since the **last** call, and each call replaces the pending arguments. Five save events in a row become one rebuild with the last path. `.flush()` runs the pending call now; `.cancel()` drops it.
* **`@throttle(interval)`** runs the function at most once per `interval`; calls inside the window do nothing and return the last result. Right for progress output and "sync at most once a minute".

To see the log lines the decorators emit, configure logging in your CLI's `main` (python-wrap never does it for you):

<!-- skip -->
```python
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
```

## Pitfalls

!!! warning "A debounced function runs on a timer thread"
    The body of a sync `@debounce` function executes on a daemon thread, not on the caller's thread, and a **pending call is lost when the process exits**. Call `.flush()` in your shutdown path (an `atexit` handler or a `finally`), as the demo does, or the final change never gets built. Exceptions raised by the debounced function do not reach the caller; log inside it.

!!! warning "`run_sync` refuses to nest"
    Calling `run_sync` while an event loop is already running in the same thread raises `RuntimeError`. That is deliberate: there is no safe way to block a running loop. In async code `await` instead, and from sync code called by async code use `asyncio.to_thread`.

* `benchmark` measures wall-clock time of the call, including any allocations and I/O. For code that is faster than a microsecond, benchmark a loop of calls inside the function. For allocation numbers use `measure_memory`.
* `profile` and `measure_memory` have overhead (and `tracemalloc` is process-wide), so use them while investigating, not permanently.
* `throttle` returns `None` until the first successful call has finished, and a call that raises still uses up its window.
* `timing` on a generator times only creating it, not iterating it.

## Test it

Time is the enemy of script tests. Patch it: `fake_clock` replaces the monotonic clock python-wrap reads, so a throttle window passes with one `advance`. For `debounce`, whose timer is a real thread timer, call `.flush()` rather than sleeping.

```python
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

import pytest

from python_wrap.testing import fake_clock


def test_timing_reports_every_call() -> None:
    timings.clear()
    load_data()
    load_data()
    assert [t.success for t in timings] == [True, True]
    assert all(t.name.endswith("load_data") for t in timings)


def test_debounce_collapses_a_burst() -> None:
    builds.clear()
    for path in ("a.py", "b.py", "c.py"):
        rebuild(path)
    rebuild.flush()  # no sleeping: run the pending call now
    assert builds == ["c.py"]  # only the last event survived
    rebuild.flush()  # nothing pending any more
    assert builds == ["c.py"]


def test_throttle_reopens_after_the_interval() -> None:
    printed.clear()
    with fake_clock() as clock:
        show_progress(1)
        show_progress(2)  # inside the window: skipped
        clock.advance(0.06)
        show_progress(3)
    assert printed == [1, 3]


def test_run_sync_cannot_nest() -> None:
    async def inner() -> None:
        with pytest.raises(RuntimeError):
            run_sync(fetch_remote_total())

    asyncio.run(inner())


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

!!! note "`RuntimeWarning` for the discarded coroutine"
    In the last test the coroutine passed to the refused `run_sync` is closed by `run_sync` itself, so you do not get a "never awaited" warning.
