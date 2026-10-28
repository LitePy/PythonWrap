# Timeout and deadline

`timeout` raises `TimeoutExpired` (a subclass of the built-in `TimeoutError`) if a call runs too long. `deadline` gives a whole block of work one shared time budget.

=== "Sync"

    ```python
    import time
    from python_wrap import TimeoutExpired, timeout

    @timeout(0.02)
    def slow() -> None:
        time.sleep(1)

    try:
        slow()
    except TimeoutExpired as exc:
        assert exc.seconds == 0.02
        assert isinstance(exc, TimeoutError)
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimeoutExpired, timeout

    @timeout(0.02)
    async def slow() -> None:
        await asyncio.sleep(1)

    async def main() -> None:
        try:
            await slow()
        except TimeoutExpired:
            pass
        else:
            raise AssertionError("expected a timeout")

    asyncio.run(main())
    ```

## When to use it

- Any call to something you do not control: HTTP, a database, a subprocess.
- Per attempt inside a [retry](retry.md), so one hung attempt cannot eat the whole budget.
- Use a `deadline` when several calls, retries and sub-steps must finish within one total time.
- Not to cut tail latency: that is [hedge](hedge.md).

## How the limit is enforced

The strategy depends on the function and the platform:

| Situation | Strategy | Stops the work? |
| --- | --- | --- |
| `async def` | `asyncio.timeout` (3.11+) or a cancellation timer (3.10) cancels the coroutine | Yes |
| Sync, Unix, main thread | `SIGALRM` timer interrupts the call | Yes |
| Sync on Windows, in a non-main thread, or `use_signals=False` | Runs in a daemon worker thread; the caller stops waiting | **No** |

`use_signals=True` forces signals (and raises `RuntimeError` where unavailable); `use_signals=False` forces the thread strategy. Nested timeouts work: the previous `SIGALRM` handler and any already-armed timer are restored afterwards.

!!! warning "The thread fallback cannot stop the work"
    When signals are unavailable, the function keeps running in the background after the timeout, and its result is discarded. Do not rely on it to cancel side effects. See [Limitations](../../project/limitations.md#timeout).

??? info "Worker thread pool"
    The worker threads are pooled: a finished worker waits (as an idle daemon thread) for the next call instead of a new thread being started per call. At most 8 idle workers are kept, an idle worker exits after about 5 seconds without work, and a worker still running a call that timed out is abandoned: it exits when that call returns and is never reused. The caller's `contextvars` are copied into the worker on every call.

## Timing out a block

The object `timeout(...)` returns is also a context manager, so you can limit a block of code without wrapping it in a function. The same object still works as a decorator.

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimeoutExpired, timeout

    async def main() -> None:
        try:
            async with timeout(0.02):
                await asyncio.sleep(1)
        except TimeoutExpired as exc:
            assert exc.seconds == 0.02
        else:
            raise AssertionError("expected a timeout")

    asyncio.run(main())
    ```

=== "Sync"

    <!-- unix-only -->
    ```python
    import time
    from python_wrap import TimeoutExpired, timeout

    try:
        with timeout(0.02):   # Unix, main thread only
            time.sleep(1)
    except TimeoutExpired:
        pass
    ```

`async with timeout(...)` uses `asyncio.timeout` on Python 3.11+ and a cancellation timer on 3.10; as with the decorator, a `TimeoutError` raised by your own code inside the block propagates unchanged.

The sync `with timeout(...)` form only exists on top of `SIGALRM`: it needs Unix and the main thread. Elsewhere (Windows, worker threads, `use_signals=False`) entering it raises `RuntimeError`, because a block of code cannot be moved to a worker thread. Decorate a function instead, which falls back to the thread strategy.

## Bounded cleanup with `cleanup_grace`

Cancelling an `async def` at the limit lets its `finally` / `except CancelledError` cleanup run, but nothing bounds that cleanup. `cleanup_grace=` gives it a budget.

```python
import asyncio
from python_wrap import TimeoutExpired, timeout

cleaned = []

@timeout(0.02, cleanup_grace=0.5)
async def work() -> None:
    try:
        await asyncio.sleep(5)
    finally:
        await asyncio.sleep(0.02)   # e.g. release a connection
        cleaned.append("released")

try:
    asyncio.run(work())
except TimeoutExpired:
    pass
assert cleaned == ["released"]
```

As a **decorator**, the call runs as a child task. At the limit it is cancelled and given `cleanup_grace` more seconds to finish; then `TimeoutExpired` is raised (also when the cleanup finished in time). A task still running after the grace is **abandoned**: a warning is logged on the package logger and the task keeps running until it ends by itself, its result discarded.

As **`async with timeout(5, cleanup_grace=1)`**, the block runs in your own task and cannot be moved, so it is not abandoned: after the grace the cleanup is cancelled a **second time** (with a warning). Cleanup that swallows cancellation cannot be stopped.

Sync functions and plain `with` blocks do not support it and raise `TypeError`: signals and worker threads cannot give cleanup a bounded grace. The grace uses real time and is not changed by the test overrides.

## Deadlines: one budget for many steps

A `timeout` limits one call. A **deadline** limits everything inside a block (several calls, retries, sub-steps) to one shared budget. `deadline(seconds)` stores an absolute expiry in a context variable, so it is visible to every function called inside the block, sync or async, and to tasks started from it. Nested deadlines never extend an outer one: the earliest wins.

A deadline does not interrupt anything by itself. Instead:

* `timeout` (decorator or block) clamps its limit to the time the deadline has left. If the deadline has already passed, the call is not started and `TimeoutExpired` is raised at once.
* [`retry`](retry.md#time-budget) never sleeps past it.
* `remaining()` returns the seconds left (or `None` outside any deadline). Pass it on, for example as an HTTP client's timeout, or use it to cap your own retry loop.
* `check()` raises `TimeoutExpired` once the deadline has passed. Call it between steps of a long job.

```python
import asyncio
from python_wrap import TimeoutExpired, timeout
from python_wrap.deadline import check, deadline, remaining

@timeout(30)                     # generous per-call limit...
async def fetch(part: int) -> int:
    await asyncio.sleep(0.01 if part < 2 else 1)
    return part

async def handle_request() -> list[int]:
    results = []
    async with deadline(0.1):    # ...but the whole request gets 0.1s
        for part in range(5):
            check()              # stop early once the budget is spent
            left = remaining()
            assert left is not None and left <= 0.1
            results.append(await fetch(part))   # clamped to what is left
    return results

async def main() -> None:
    try:
        await handle_request()
    except TimeoutExpired as exc:
        assert exc.seconds <= 0.1
    else:
        raise AssertionError("expected the deadline to run out")

asyncio.run(main())
```

## Events

Every expiry emits `timeout.expired` with the field `seconds` (the limit). See [Metrics](../observability/metrics.md).

## Testing

`fast_mode()` and `fake_clock()` do not change timeouts: the limit uses real timers, so that "wait at most 5 s" does not turn into "fail at once". Test with tiny real durations, as the examples on this page do. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Import the deadline helpers from the module"
    Use `from python_wrap.deadline import check, deadline, remaining`. The package attribute `python_wrap.deadline` is the `deadline` function itself (the same is true of `retry` and `policy`), so `import python_wrap.deadline as m` does not give you the module.

## See also

- [Retry](retry.md), [Hedge](hedge.md), [Policies](policies.md)
- [Errors](../../get-started/errors.md) for `TimeoutExpired`
- [API reference](../../api/deadline.md)
