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

## Per key and per instance

By default the window (throttle) or pending call (debounce) belongs to the decorated function. Two opt-ins split it:

- `key=` is a function of the call's arguments (including `self` on methods), as in `rate_limit` and `singleflight`. Each key gets its own window or countdown. Idle keys are dropped.
- `per_instance=True` (methods only) gives each instance its own window or pending call. State is held through a weak reference, so instances stay collectable and do not need to be hashable. Instances must be weak-referenceable (a `__slots__` class must list `"__weakref__"`), otherwise the call raises `TypeError`.

They combine: with both, the key is applied inside each instance.

```python
from python_wrap import debounce, throttle

class Sensor:
    def __init__(self, name: str) -> None:
        self.name = name

    @throttle(60, per_instance=True)
    def read(self) -> str:
        return self.name

a, b = Sensor("a"), Sensor("b")
assert a.read() == "a"
assert b.read() == "b"      # its own window: not skipped by a's

@throttle(60, key=lambda user, *_: user)
def notify(user: str, text: str) -> str:
    return f"{user}:{text}"

assert notify("ana", "1") == "ana:1"
assert notify("ben", "1") == "ben:1"   # another key: runs
assert notify("ana", "2") == "ana:1"   # same key, inside the window: skipped

class Doc:
    def __init__(self) -> None:
        self.saved: list[str] = []

    @debounce(60, per_instance=True)
    def save(self, text: str) -> None:
        self.saved.append(text)

d1, d2 = Doc(), Doc()
d1.save("one")
d2.save("two")
Doc.save.flush(d1)          # with per_instance or key=, cancel/flush take the call's arguments
assert d1.saved == ["one"] and d2.saved == []
Doc.save.cancel(d2)
assert d2.saved == []
```

With `key=` or `per_instance=`, `.cancel(*args, **kwargs)` and `.flush(*args, **kwargs)` take the arguments of a call to pick the slot (for a method, pass the instance first when reaching them from the class: `Doc.save.flush(d1)`). Called with no arguments they act on every slot, and `.flush()` then returns `None` because there is no single result. Async `flush` is still a coroutine to `await`.

## Events

`throttle` emits `throttle.skipped` (no fields) for each skipped call. `debounce` emits no events. See [Metrics](../observability/metrics.md).

## Testing

`disable_rate_limits()` also makes `throttle` let every call through. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Debounce runs on another thread"
    Sync debounced functions run on a daemon timer thread, so the function body runs on a different thread from the caller. Async debounce waits with `asyncio.sleep`, so calls must be awaited (or wrapped in tasks) for it to take effect. Both wait in real time.

!!! note "State belongs to the function"
    Throttle and debounce state belongs to the decorated function. On a **method** it is shared by every instance unless you split it with `per_instance=True` (one window or pending call per instance, held through a weak reference) or `key=` (one per key, a function of the call's arguments). See [Per key and per instance](#per-key-and-per-instance) and [Limitations](../../project/limitations.md#shared-state-on-methods).

    ```python
    from python_wrap import debounce, throttle

    class Sensor:
        @throttle(60, per_instance=True)
        def read(self) -> str: ...

    @debounce(0.5, key=lambda doc_id, *_: doc_id)
    def autosave(doc_id: int, text: str) -> None: ...
    ```

    With a key or `per_instance`, `autosave.cancel(doc_id, "")` and `autosave.flush(doc_id, "")` take a call's arguments to pick the slot; without arguments they act on all of them.

## See also

- [Rate limits](rate-limits.md), [Singleflight](../caching/singleflight.md)
- [API reference](../../api/concurrency.md)
