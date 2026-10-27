# Memoize

Cache a function's result per argument values, so repeated calls with the same arguments skip the work.

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

## When to use it

- A function is pure (or close enough) and called repeatedly with the same arguments.
- An answer is good for a while: add `ttl`.
- Not for one result regardless of arguments: use [`once`](once.md).
- Not for a per-object attribute: use [`cached_property`](cached-property.md).
- Not when you only want to merge calls that are running right now: use [`singleflight`](singleflight.md).

## How keys work

- Arguments are bound to the function signature and defaults are applied, so `f(1)`, `f(x=1)` and `f(1, y=<default>)` share one entry.
- Lists, dicts and sets (also nested) are converted to hashable forms automatically, and `[1]` and `(1,)` do not collide.
- Any other unhashable argument raises `TypeError` at call time. Pass `key=` to build the key yourself.
- Exceptions are not cached by default; see [Caching exceptions](#caching-exceptions).

```python
from python_wrap import memoize

@memoize
def total(items: list[int]) -> int:
    return sum(items)

assert total([1, 2, 3]) == 6
assert total([1, 2, 3]) == 6
assert total.cache_info().hits == 1
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

## Expiry and size

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
| `cache_exceptions` | Exception class (or tuple) to cache like a result. |
| `exception_ttl` | Seconds a cached exception stays valid (defaults to `ttl`). |

### Per-item TTL

`ttl` may also be a function of the result, evaluated when the result is stored. Return `None` for "never expires"; a zero or negative value means "do not store".

```python
from python_wrap import memoize

@memoize(ttl=lambda token: token["expires_in"])
def fetch_token(scope: str) -> dict:
    return {"scope": scope, "expires_in": 3600}

fetch_token("read")
assert fetch_token.cache_contains("read")
```

## Caching exceptions

List the exceptions that are a stable answer (a missing record, a rejected input) in `cache_exceptions`. They are stored for `exception_ttl` seconds (default: `ttl`) and re-raised on every hit as the **same exception instance**, so its traceback grows with each raise. `asyncio.CancelledError` is never cached. When `ttl` is a function you must pass `exception_ttl` too.

```python
from python_wrap import memoize

calls = []

@memoize(cache_exceptions=KeyError, exception_ttl=30)
def lookup(key: str) -> str:
    calls.append(key)
    raise KeyError(key)

for _ in range(3):
    try:
        lookup("x")
    except KeyError:
        pass
assert calls == ["x"]
```

## Controlling the cache

Every memoized function has these helpers:

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

Inspect without side effects (nothing is computed, counted or moved in the LRU order):

```python
from python_wrap import memoize

@memoize(maxsize=8)
def triple(n: int) -> int:
    return n * 3

assert not triple.cache_contains(2)
triple(2)
assert triple.cache_contains(2)
assert triple.cache_get(2) == 6              # raises KeyError when absent
assert triple.cache_parameters()["maxsize"] == 8
```

`cache_get` is typed `Any` because for `async def` functions it returns the awaited result, not a coroutine.

## Async and methods

**Async.** Concurrent awaits of a missing key share one in-flight task per event loop, so the function runs once.

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
    results = await asyncio.gather(square(4), square(4), square(n=4))
    assert results == [16, 16, 16]
    assert calls == [4]

asyncio.run(main())
```

**Methods.** `self` is part of the key, so results are per instance. On a method, `cache_delete`, `cache_contains` and `cache_get` take the instance as the first argument, because `obj.method` is an ordinary bound method and its attributes are not bound: `obj.method.cache_get(obj, x)`.

```python
from python_wrap import memoize

class Repo:
    @memoize
    def load(self, key: int) -> int:
        return key * 2

repo = Repo()
repo.load(42)
assert repo.load.cache_contains(repo, 42)
assert repo.load.cache_get(repo, 42) == 84
assert repo.load.cache_delete(repo, 42) is True
```

## The `cache` alias

`cache(duration, maxsize=None)` is v0.1's name for `memoize(ttl=duration, maxsize=maxsize)`, kept for compatibility and soft-deprecated. Prefer `memoize(ttl=...)` in new code (`cache` also clashes with `functools.cache`).

```python
import datetime
from python_wrap import cache

@cache(datetime.timedelta(minutes=5))
def load() -> int:
    return 42

assert load() == 42
```

Unlike v0.1, every decorated function gets its own store, even when one `cache(...)` result is applied to several functions.

## Events

`memoize` (and `cache`) emit `cache.hit`, `cache.miss` and `cache.evicted` (field `reason`: `"lru"` or `"ttl"`) to `python_wrap.events` listeners, with the function's qualified name. With no listener registered this costs one list check. See [Metrics](../observability/metrics.md) for recipes.

## Testing

Use `python_wrap.testing.disable_caching()` to bypass every cache in tests.

## Gotchas

!!! warning "Two threads can compute the same key"
    The cache is guarded by a lock, but the function is never called while holding it. For sync functions, two threads that miss on the same key at the same moment may both compute the value, and the last result stored wins. If that is not acceptable, stack [`singleflight`](singleflight.md) under `memoize`.

!!! warning "Methods keep instances alive"
    `self` is part of the key and the cache holds a strong reference to every instance it has seen, which keeps them alive. Use `maxsize` or `ttl` (or a `key=` that ignores `self`) on methods of many short-lived objects. See [Limitations](../../project/limitations.md).

!!! warning "Unhashable instances"
    A method of a class whose instances are unhashable (for example an `eq=True` dataclass) fails with a `TypeError` naming `self`. `id(self)` is deliberately not used as a fallback because ids are reused after garbage collection. Pass `key=` or use [`cached_property`](cached-property.md) instead.

## See also

- [Once](once.md), [Cached property](cached-property.md), [Singleflight](singleflight.md)
- [API reference](../../api/caching.md)
