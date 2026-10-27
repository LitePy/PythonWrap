# Timing

Measure how long calls take, how much they allocate, and where the time goes.

```python
from python_wrap import TimingRecord, timing

records: list[TimingRecord] = []

@timing(logger="demo", threshold=0.5, callback=records.append)
def fast() -> int:
    return 1

fast()
assert records[0].success is True
assert records[0].name.endswith("fast")
assert records[0].elapsed < 0.5
```

## When to use it

- `timing` to log or collect the duration of every call in production.
- `benchmark` to compare implementations in a script or test.
- `measure_memory` and `profile` to investigate one slow or hungry function.
- For counts, error rates and percentiles over time, use [`count_calls`](metrics.md#count_calls).

## `timing`

Measure and log how long each call takes. Async functions are timed across the whole awaited coroutine. A call that raises is logged as `<name> raised ValueError after 12.34 ms` and the exception propagates.

=== "Sync"

    ```python
    from python_wrap import TimingRecord, timing

    records: list[TimingRecord] = []

    @timing(callback=records.append)
    def work() -> int:
        return 1

    work()
    assert records[0].success is True
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimingRecord, timing

    records: list[TimingRecord] = []

    @timing(callback=records.append)
    async def pause() -> None:
        await asyncio.sleep(0.02)

    asyncio.run(pause())
    assert records[0].elapsed >= 0.015   # the awaited time is measured
    ```

| Option | Meaning |
| --- | --- |
| `threshold` | Only log successful calls slower than this many seconds. Failed calls are always logged. |
| `callback` | Called with a `TimingRecord(name, elapsed, success)` for **every** call, regardless of `threshold`. Use it to feed a metrics system. Errors in the callback are logged and swallowed. |
| `logger`, `level` | Where it logs; `INFO` by default. See [Configuring logging](logging.md#configuring-logging). |

`timing` uses `time.perf_counter`.

## `benchmark`

Run a function repeatedly and get statistics. It is a **plain function, not a decorator** (see [Migration](../../project/migration.md)).

=== "Sync"

    ```python
    from python_wrap import benchmark

    result = benchmark(sorted, args=([3, 1, 2],), runs=10, warmup=2)
    assert result.runs == 10
    assert result.min <= result.median <= result.max
    print(result)   # sorted: 10 runs, mean 1.2 us, median 1.1 us, ...
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import benchmark_async

    async def work(n: int) -> int:
        await asyncio.sleep(0)
        return n

    async def main() -> None:
        result = await benchmark_async(work, args=(1,), runs=3)
        assert result.runs == 3

    asyncio.run(main())
    ```

`BenchmarkResult` has `name`, `runs`, `timings`, `mean`, `median`, `min`, `max` and `stdev`, all in seconds. `benchmark` raises `TypeError` for an `async def` function (use `benchmark_async`).

## `measure_memory`

Measure the Python memory a call allocates, using `tracemalloc`. Works on every OS and logs `<name> peak memory 1.00 MiB (net +12.00 KiB)`.

```python
from python_wrap import MemoryRecord, measure_memory

records: list[MemoryRecord] = []

@measure_memory(callback=records.append)
def build() -> list[int]:
    return [0] * 100_000

data = build()
assert records[0].peak_bytes >= 100_000 * 8 * 0.5   # at least roughly the list's size
assert records[0].net_bytes > 0                     # the list is still alive
```

`peak_bytes` is the highest traced allocation during the call, relative to its start; `net_bytes` is the memory still held at the end minus at the start (it can be negative).

!!! note
    `tracemalloc` is process-global. Allocations by other threads or tasks while the call runs are counted too, and tracing slows the program. See [Limitations](../../project/limitations.md).

## `profile`

Profile each call with `cProfile` and log the top rows.

```python
import pstats
from python_wrap import profile

captured: list[pstats.Stats] = []

@profile(sort="tottime", limit=5, callback=captured.append)
def work() -> int:
    return sum(i * i for i in range(1000))

work()
assert isinstance(captured[0], pstats.Stats)
```

`sort` is any `pstats.SortKey` value (`"cumulative"` by default, or `"tottime"`, `"calls"`, ...); `limit` is the number of rows logged (`None` for all). If another profiler is already active, a warning is logged once and the function runs unprofiled.

## See also

- [Metrics and events](metrics.md) for in-process counts and percentiles
- [Logging](logging.md)
- [API reference](../../api/timing.md)
