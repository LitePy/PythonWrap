# Throttle and debounce

Collapse bursts of calls. `throttle` runs the function at most once per interval; `debounce` runs it once, after the calls have stopped.

```python
from python_wrap import throttle

runs = []

@throttle(60)
def refresh() -> int:
    runs.append(1)
    return len(runs)

assert refresh() == 1
assert refresh() == 1      # skipped: returns the previous result
assert len(runs) == 1
```

## When to use it

- `throttle`: "update the progress bar", "sync at most once a minute". The first call runs now; later calls inside the window are skipped.
- `debounce`: "save after the user stops typing". Only the final call of a burst runs, after a quiet period.
- To reject or delay excess calls instead of skipping them silently, use [`rate_limit`](rate-limits.md).

## Throttle

Run at most once per `interval` seconds. The first call runs and opens a window; calls inside the window do not run and return **the result of the last call that did run** (`None` if none has finished yet, or it raised).

=== "Sync"

    ```python
    from python_wrap import throttle

    runs = []

    @throttle(60)
    def refresh() -> int:
        runs.append(1)
        return len(runs)

    assert refresh() == 1
    assert refresh() == 1      # skipped: returns the previous result
    assert len(runs) == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import throttle

    runs = []

    @throttle(60)
    async def refresh() -> int:
        runs.append(1)
        return len(runs)

    async def main() -> None:
        assert await refresh() == 1
        assert await refresh() == 1
        assert len(runs) == 1

    asyncio.run(main())
    ```

A call that raises still uses up its window, and the error goes only to its own caller. The wrapped function returns `R | None` for the type checker.

## Debounce

Postpone running until `wait` seconds have passed since the **last** call. Each call restarts the countdown and replaces the pending arguments, so only the final call of a burst runs.

=== "Sync"

    ```python
    import time
    from python_wrap import debounce

    saved = []

    @debounce(0.02)
    def save(text: str) -> None:
        saved.append(text)

    save("a")
    save("ab")
    save("abc")            # each call returns None immediately
    time.sleep(0.04)       # the timer thread runs the last call
    assert saved == ["abc"]
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import debounce

    saved = []

    @debounce(0.02)
    async def save(text: str) -> str:
        saved.append(text)
        return text

    async def main() -> None:
        # Every caller in a burst awaits the same outcome: the final call's result.
        results = await asyncio.gather(save("a"), save("ab"), save("abc"))
        assert results == ["abc", "abc", "abc"]
        assert saved == ["abc"]

    asyncio.run(main())
    ```

### Cancel and flush

The wrapper also has:

- `.cancel()` drops the pending call (async awaiters get `CancelledError`).
- `.flush()` runs the pending call right now and returns its result, or `None` if nothing is pending. For `async def` functions it is a coroutine to `await`.

```python
from python_wrap import debounce

saved = []

@debounce(60)
def save(text: str) -> None:
    saved.append(text)

save("draft")
save("final")
save.flush()                 # run now instead of waiting a minute
assert saved == ["final"]

save("never saved")
save.cancel()                # drop it
assert saved == ["final"]
```

## Events

`throttle` emits `throttle.skipped` (no fields) for each skipped call. `debounce` emits no events. See [Metrics](../observability/metrics.md).

## Testing

`disable_rate_limits()` also makes `throttle` let every call through. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Debounce runs on another thread"
    Sync debounced functions run on a daemon timer thread, so the function body runs on a different thread from the caller. Async debounce waits with `asyncio.sleep`, so calls must be awaited (or wrapped in tasks) for it to take effect. Both wait in real time.

!!! note "State belongs to the function"
    Throttle and debounce state belongs to the decorated function. On a **method** it is shared by every instance. See [Limitations](../../project/limitations.md#shared-state-on-methods).

## See also

- [Rate limits](rate-limits.md), [Singleflight](../caching/singleflight.md)
- [API reference](../../api/concurrency.md)
