# Caching

Three decorators, three different questions: "have I computed this before?" (`memoize`), "for how long is the answer good?" (`memoize(ttl=...)`), and "have I done this setup yet?" (`once`).

## `memoize`

Cache a function's result per argument values.

=== "Sync"

    ```python
    from python_wrap import memoize

    calls = []

    @memoize
    def square(n: int) -> int:
        calls.append(n)
        return n * n

    assert square(4) == 16
    assert square(n=4) == 16  # arguments are bound to the signature: same entry
    assert calls == [4]
    assert square.cache_info().hits == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import memoize

    calls = []

    @memoize
    async def square(n: int) -> int:
        calls.append(n)
        await asyncio.sleep(0.01)
        return n * n

    async def main() -> None:
        # Concurrent awaits of a missing key share one in-flight task.
        results = await asyncio.gather(square(4), square(4), square(n=4))
        assert results == [16, 16, 16]
        assert calls == [4]

    asyncio.run(main())
    ```

How keys work:

- Arguments are bound to the function signature and defaults are applied, so `f(1)`, `f(x=1)` and `f(1, y=<default>)` share one entry.
- Lists, dicts and sets (also nested) are converted to hashable forms automatically, and `[1]` and `(1,)` do not collide.
- Any other unhashable argument raises `TypeError` at call time. Pass `key=` to build the key yourself.
- Exceptions are never cached.

```python
from python_wrap import memoize

@memoize
def total(items: list[int]) -> int:
    return sum(items)

assert total([1, 2, 3]) == 6
assert total([1, 2, 3]) == 6
assert total.cache_info().hits == 1
```

### Expiry and size

```python
import time
from python_wrap import memoize

calls = []

@memoize(maxsize=2, ttl=0.02)
def load(key: str) -> str:
    calls.append(key)
    return key.upper()

load("a"); load("b"); load("c")          # "a" is evicted (least recently used)
assert load.cache_info().currsize == 2
assert load.cache_info().maxsize == 2

time.sleep(0.03)                          # entries expire after ttl
assert load.cache_info().currsize == 0
load("c")
assert calls == ["a", "b", "c", "c"]
```

| Option | Meaning |
| --- | --- |
| `maxsize` | Maximum entries; least recently used are evicted. `None` is unbounded. |
| `ttl` | Seconds (or a `datetime.timedelta`) an entry stays valid. `None` never expires. |
| `typed` | If `True`, `f(1)` and `f(1.0)` are cached separately. |
| `key` | Custom `key(*args, **kwargs)` returning a hashable key. |

### Controlling the cache

Every memoized function has:

```python
from python_wrap import memoize

@memoize
def double(n: int) -> int:
    return n * 2

double(1); double(2)
assert double.cache_info().currsize == 2     # CacheInfo(hits, misses, maxsize, currsize)
assert double.cache_delete(1) is True        # drop one entry
assert double.cache_delete(1) is False       # it was not there
double.cache_clear()                         # drop everything and reset the statistics
assert double.cache_info() == (0, 0, None, 0)
```

### A custom key

Ignore arguments that do not change the answer, or accept arguments that are not hashable:

```python
from python_wrap import memoize

class Client: ...

@memoize(key=lambda client, user_id: user_id)  # ignore the client object
def fetch_user(client: Client, user_id: int) -> dict:
    return {"id": user_id}

fetch_user(Client(), 1)
fetch_user(Client(), 1)
assert fetch_user.cache_info().hits == 1
```

### Caching in the face of threads

The cache is guarded by a lock, but the function is never called while holding it. For sync functions, two threads that miss on the same key at the same moment may both compute the value, and the last result stored wins. If that is not acceptable, stack [`singleflight`](concurrency.md#singleflight) under `memoize`.

### Methods

`self` is part of the key, so results are per instance. The cache holds a strong reference to every instance it has seen, which keeps them alive. See [Limitations](../limitations.md#memoize-on-methods).

## `cache`

`cache(duration, maxsize=None)` is v0.1's name for `memoize(ttl=duration, maxsize=maxsize)`, kept for compatibility. Prefer `memoize(ttl=...)` in new code (and note that `cache` also clashes with `functools.cache`).

```python
import datetime
from python_wrap import cache

@cache(datetime.timedelta(minutes=5))
def load() -> int:
    return 42

assert load() == 42
```

Unlike v0.1, every decorated function gets its own store, even when one `cache(...)` result is applied to several functions.

## `once`

Run a function a single time; later calls return the first result and ignore their arguments.

=== "Sync"

    ```python
    from python_wrap import once

    @once
    def setup() -> object:
        return object()

    assert setup() is setup()
    assert setup.called is True

    setup.reset()          # forget the result; the next call runs again
    assert setup.called is False
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import once

    runs = []

    @once
    async def connect() -> str:
        runs.append(1)
        await asyncio.sleep(0.01)
        return "connection"

    async def main() -> None:
        a, b = await asyncio.gather(connect(), connect())  # share one task
        assert a == b == "connection"
        assert runs == [1]

    asyncio.run(main())
    ```

`once` is thread-safe. If the first call raises, the function is not marked done and the next call tries again.

## Which one?

| You want | Use |
| --- | --- |
| Same arguments give the same answer forever | `@memoize` (add `maxsize` so it cannot grow without bound) |
| The answer goes stale | `@memoize(ttl=...)` |
| One result regardless of arguments (setup, config load) | `@once` |
| No cache, but do not run the same in-flight call twice | [`@singleflight`](concurrency.md#singleflight) |
