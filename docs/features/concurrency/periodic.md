# Periodic tasks

Run a function every N seconds in the background, and stop it cleanly. `periodic` is a small loop, not a scheduler: no cron expressions, no persistence, no catching up on missed runs.

```python
import time
from python_wrap.periodic import periodic

beats: list[int] = []

@periodic(0.01, max_runs=3)
def heartbeat() -> None:
    beats.append(1)

with heartbeat.running_in_background():
    while heartbeat.runs < 3:
        time.sleep(0.005)

assert not heartbeat.running     # stopped and joined on leaving the block
assert len(beats) == 3
heartbeat()                      # still an ordinary function
```

## When to use it

- Refresh a cache, flush a buffer, send a heartbeat, poll a queue, sweep expired rows.
- You want clean shutdown: no daemon thread left running after a test or after a `with` block.
- You need cron expressions, persistence or distributed locks: use a real scheduler.

## What you get

| | |
| --- | --- |
| `f.start(*args, **kwargs)` | Start the loop. The arguments are passed to every run. Returns `f`. Raises `RuntimeError` if already running. |
| `f.stop()` | Plain functions: stop and join the thread (`timeout=5.0`), returns `True` once it ended. `async def`: `await f.stop()` cancels the task and awaits it. Safe to call twice or before `start()`. |
| `f.running` | `True` while the thread or task is alive. |
| `f.runs` | Runs made since `start()`, failed ones included. |
| `f.last_error` | Exception of the most recent failed run, or `None`. |
| `f.running_in_background(*args, **kwargs)` | `with` (or `async with`) block that starts the loop and stops it on exit. |
| `f(*args)` | Calls the function directly, as before. |

## Plain functions and async functions

A plain function runs in a **daemon thread** (named `python-wrap-periodic:<name>`), so it never keeps the interpreter alive. The caller's `contextvars` are copied into it.

An `async def` function runs as a **task on the current event loop**, so `start()` must be called from inside a running loop; anywhere else it raises a `RuntimeError` saying so. `stop()` is a coroutine: it cancels the task, including a run in progress, and waits for it. If the task awaiting `stop()` is itself cancelled (or an `asyncio.timeout` around it expires), that `CancelledError` propagates at once while the loop finishes its cleanup in the background. Called from inside the function, `stop()` ends the loop as soon as that run returns, without waiting for the next tick.

```python
import asyncio
from python_wrap.periodic import periodic

@periodic(0.01)
async def refresh() -> None:
    ...

async def main() -> None:
    async with refresh.running_in_background():
        await asyncio.sleep(0.05)
    assert not refresh.running

asyncio.run(main())
```

A thread cannot be interrupted, so `stop()` on a plain function lets the run in progress finish. If it does not finish within `timeout`, `stop()` logs a warning and returns `False`.

## Timing

- **Start to start, no drift.** The n-th run is due at `first + n * interval`, whatever the runs cost.
- **Missed ticks are skipped.** If a run takes longer than the interval, the loop does not run again in a burst to catch up. It waits for the next tick on the grid.
- **`initial_delay`** delays the first run (default: run at once).
- **`jitter`** (a fraction, `0` up to but excluding `1`) moves each run by a uniformly random amount up to plus or minus `jitter * interval`. It spreads out many processes that started together. Ticks are computed without it, so it never accumulates.
- **`max_runs`** makes the loop end by itself after that many runs.

## Errors

| `on_error` | Effect of an exception in a run |
| --- | --- |
| `"log"` (default) | Logged through the package logger, loop continues. |
| `"stop"` | Logged, loop ends. |
| `"raise"` | Loop ends and the exception is re-raised once from `stop()` (or on leaving the `with` block), so it is never lost in a background thread. If `start()` is called again before `stop()` collected it, the old exception is logged at `ERROR` (with its traceback) and then dropped. |

In every mode the exception is kept in `last_error` and published as a `periodic.failed` event. `logger=` and `level=` (default `logging.ERROR`) control the log line.

## Methods

Decorating a method in a class body is rejected with a `TypeError`, because the loop needs an instance. Decorate the bound method instead:

```python
from python_wrap.periodic import periodic

class Cache:
    def __init__(self) -> None:
        self.refreshed = 0
        self.job = periodic(60, name="cache-refresh")(self.refresh)

    def refresh(self) -> None:
        self.refreshed += 1

cache = Cache()
cache.refresh()
assert cache.job.runs == 0     # not started yet; call cache.job.start()
```

## Events

`periodic.run` (`run`, `seconds`) after each successful run and `periodic.failed` (`run`, `exception`) after each failure; `Event.function` is `name=` or the function's qualified name. See [Metrics and events](../observability/metrics.md).

## Testing

The schedule uses **real time**. `fast_mode()` and `fake_clock()` affect only backoff waits (see [what the overrides affect](../../testing/index.md#what-the-overrides-affect)); making a periodic loop instant would just spin the CPU. In tests, call the function directly, or use a tiny interval, `max_runs` and `with f.running_in_background():` so nothing outlives the test.

## See also

- [Throttle and debounce](throttle-debounce.md), [run_in_thread](run-in-thread.md)
- [Retry](../resilience/retry.md) for retrying a single failing run
- [API reference](../../api/periodic.md)
