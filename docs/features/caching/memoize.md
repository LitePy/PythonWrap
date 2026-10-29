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
- An answer is good for a while: add `ttl`; add `stale_ttl` to keep serving it while it refreshes in the background.
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
| `stale_ttl` | Seconds after `ttl` during which a stale value is still served while it is refreshed in the background. See [Serving stale values](#serving-stale-values-while-refreshing). |
| `store` | A [`CacheStore`](#custom-stores) to keep entries in instead of the built-in dict. |
| `per_instance` | For methods: one cache per instance, without keeping instances alive. See [Per-instance method caches](#per-instance-method-caches). |
| `logger` | Logger (or name) for failed background refreshes. |

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

## Serving stale values while refreshing

With `stale_ttl`, an entry older than `ttl` is *stale* but not gone. For `stale_ttl` more seconds a call still returns it immediately, and the call that finds it stale starts **one** background refresh. Once a value has been loaded, callers never wait for a slow backend again; after the refresh lands they get the new value.

```python
import time
from python_wrap import memoize

version = 1

@memoize(ttl=0.05, stale_ttl=60)
def settings() -> int:
    return version                # imagine a slow HTTP call

assert settings() == 1            # miss: computed in the caller
version = 2
time.sleep(0.06)                  # older than ttl: now stale
assert settings() == 1            # served at once; a refresh starts in the background
time.sleep(0.05)
assert settings() == 2            # the refreshed value
```

- **Sync functions** refresh in a daemon thread, at most one per key, running in a copy of the caller's `contextvars` context (so request ids and similar context reach it).
- **`async def` functions** refresh in a task on the running event loop. It shares the in-flight bookkeeping, so concurrent stale hits and misses of one key start a single task.
- **A failed refresh keeps the stale value.** It is logged at `WARNING` (on `logger`, default `python_wrap`), emits `cache.refresh_failed`, and the next call starts another refresh.
- **Past `ttl + stale_ttl`** the entry is dropped and the next call is an ordinary miss, computed in the caller.
- `stale_ttl` requires `ttl` (a number, a `timedelta` or a [function of the result](#per-item-ttl)). Cached exceptions are never served stale.

Refresh decisions read the package clock, so `fake_clock` drives them in tests without waiting:

```python
import threading
from python_wrap import memoize
from python_wrap.testing import fake_clock

calls = []

@memoize(ttl=10, stale_ttl=50)
def rates() -> int:
    calls.append(1)
    return len(calls)

with fake_clock() as clock:
    assert rates() == 1
    clock.advance(15)
    assert rates() == 1           # stale: served, refresh started
    for t in threading.enumerate():
        if t.name.startswith("python_wrap-refresh-"):
            t.join()
    assert rates() == 2
```

## Custom stores

By default entries live in a dict inside the decorator. Pass `store=` to keep them somewhere else: a dict you own, a shared registry, Redis. A store is any object with these five methods (the `CacheStore` protocol; no base class needed):

| Method | Contract |
| --- | --- |
| `get(key)` | The entry stored under `key`, or `None`. |
| `set(key, entry, ttl)` | Store `entry`. `ttl` is the number of seconds after which it is useless and may be discarded (`None`: keep it). |
| `delete(key)` | Remove the entry; return `True` if there was one. |
| `clear()` | Remove every entry. |
| `__len__()` | The number of stored entries (`cache_info().currsize`). |

```python
from python_wrap import memoize

class DictStore:
    def __init__(self) -> None:
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, entry, ttl):
        self.data[key] = entry

    def delete(self, key):
        return self.data.pop(key, None) is not None

    def clear(self):
        self.data.clear()

    def __len__(self):
        return len(self.data)

store = DictStore()

@memoize(store=store, ttl=300)
def price(sku: str) -> float:
    return 9.99

price("A-1"); price("A-1")
assert len(store) == 1
assert price.cache_info().hits == 1
assert price.cache_get("A-1") == 9.99   # the cache_* helpers all work
price.cache_clear()
assert len(store) == 0
```

What a store can rely on, and what it must do:

- **Entries are opaque.** Return exactly what was stored and never look inside. A store that serialises (Redis, files) must round-trip entries, for example with `pickle`. Entries are never `None`.
- **Entries carry their own deadlines**, as Unix timestamps, and `memoize` checks them. A store may forget an entry at any time (that is just a miss) and does not have to implement expiry, though honouring `ttl` keeps it from filling up. Processes sharing a store agree on expiry as long as their clocks agree.
- **Keys are hashable, but not strings.** They are tuples of the normalised arguments (or whatever `key=` returns). A store that needs `str` or `bytes` keys must serialise them itself, with a prefix per function when several functions share one backend.
- **The store must be thread-safe.** `memoize` does not hold its lock while calling the store, so a network round trip never blocks other callers. Single `dict` operations are atomic in CPython, so the store above qualifies.
- **Methods are synchronous.** For `async def` functions they run on the event loop, so keep them fast.
- Exceptions raised by the store propagate to the caller.
- `maxsize` (LRU) only applies to the built-in store: passing both `store=` and `maxsize=` raises `TypeError`.

Use the shipped `RedisStore` when a cache needs to be shared between workers:

<!-- skip -->
```python
import redis
from python_wrap import RedisStore, memoize

client = redis.Redis(socket_timeout=2, socket_connect_timeout=2)

@memoize(store=RedisStore(client, prefix="pricing:v1:", fail_open=False), ttl=300)
def price(sku: str) -> float:
    return load_price(sku)
```

Keys are hashed by default and `clear()` escapes the namespace before scanning it.
`cache_info().currsize` is **None** for `RedisStore`: statistics do not scan Redis.
External deadlines use the live Unix wall clock. The adapter defaults to pickle for
values, so only decode data from a trusted store or provide a safe application serializer.

Store methods are synchronous and a network-backed store can block an async event loop.
Configure finite client timeouts and use a local cache for latency-sensitive async code.
See [shared caches and credentials](stores-and-credentials.md) for serialization,
failure modes, unknown counts and credential refresh.

## Per-instance method caches

On a plain `@memoize` method, `self` is part of the key: the cache holds a strong reference to every instance, keeping it alive, and instances must be hashable. `per_instance=True` fixes both. Each instance gets its own cache, found through a weak reference and keyed by the arguments after `self`:

```python
import dataclasses
import gc
import weakref
from python_wrap import memoize

@dataclasses.dataclass          # eq=True: instances are unhashable
class Account:
    id: int

    @memoize(per_instance=True, maxsize=100)
    def balance(self, currency: str) -> float:
        return 100.0

a, b = Account(1), Account(1)   # equal, but two objects: two caches
a.balance("EUR"); a.balance("EUR"); b.balance("EUR")
assert Account.balance.cache_info().hits == 1
assert Account.balance.cache_info().currsize == 2   # summed over live instances

ref = weakref.ref(a)
del a
gc.collect()
assert ref() is None                                # the cache did not keep it alive
assert Account.balance.cache_info().currsize == 1   # its entries went with it
```

- **Identity, not equality.** Caches belong to objects, so two equal instances never share results, and unhashable classes (an `eq=True` dataclass) work. When an instance is garbage-collected its cache is dropped, so a later object that gets the same `id()` starts empty.
- **Instances must be weak-referenceable.** A class with `__slots__` must list `"__weakref__"`; otherwise the call raises `TypeError`.
- `maxsize` applies to each instance. `cache_info()` adds up hits, misses and sizes over every live instance and `cache_clear()` clears them all. `cache_delete`, `cache_get` and `cache_contains` take the instance first, as on any method: `obj.balance.cache_get(obj, "EUR")`.
- A custom `key=` still receives `self`; leave it out of the key, since the cache is already per instance.
- A cached result that references its own instance (returning `self`, a bound method, a closure over `self`) keeps that instance alive.
- `per_instance` uses the built-in store and cannot be combined with `store=`.

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

`memoize` (and `cache`) emit `cache.hit`, `cache.miss` and `cache.evicted` (field `reason`: `"lru"` or `"ttl"`) to `python_wrap.events` listeners, with the function's qualified name. With `stale_ttl` they also emit `cache.stale` (a stale value was served and a background refresh started; the hit itself is reported as `cache.hit`) and `cache.refresh_failed` (field `exception`). With no listener registered this costs one list check. See [Metrics](../observability/metrics.md) for recipes.

## Testing

Use `python_wrap.testing.disable_caching()` to bypass every cache in tests: nothing is read, written or refreshed. `ttl` and `stale_ttl` run on the package clock, so `python_wrap.testing.fake_clock()` makes entries expire or turn stale without waiting (see the example under [Serving stale values](#serving-stale-values-while-refreshing)).

## Gotchas

!!! warning "Two threads can compute the same key"
    The cache is guarded by a lock, but the function is never called while holding it. For sync functions, two threads that miss on the same key at the same moment may both compute the value, and the last result stored wins. If that is not acceptable, stack [`singleflight`](singleflight.md) under `memoize`.

!!! warning "Methods keep instances alive"
    `self` is part of the key and the cache holds a strong reference to every instance it has seen, which keeps them alive. Use [`per_instance=True`](#per-instance-method-caches) on methods of many short-lived objects (or `maxsize`, `ttl`, or a `key=` that ignores `self`). See [Limitations](../../project/limitations.md).

!!! warning "Unhashable instances"
    A method of a class whose instances are unhashable (for example an `eq=True` dataclass) fails with a `TypeError` naming `self`. `id(self)` is deliberately not used as a fallback because ids are reused after garbage collection. Use [`per_instance=True`](#per-instance-method-caches) (keyed by identity through a weak reference), pass `key=`, or use [`cached_property`](cached-property.md) instead.

## See also

- [Once](once.md), [Cached property](cached-property.md), [Singleflight](singleflight.md)
- [API reference](../../api/caching.md)
