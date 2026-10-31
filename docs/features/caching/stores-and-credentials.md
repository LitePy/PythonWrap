# Shared caches and expiring credentials

Use `memoize` for ordinary results, `RedisStore` to share them between processes, and
`expiring` for loaders that return a credential with a relative lifetime.

## Redis cache adapter

`RedisStore` takes an existing synchronous Redis client; python-wrap does not import or
install `redis`. Use a separate prefix for each cached function.

<!-- skip -->
```python
import redis
from python_wrap import RedisStore, memoize

client = redis.Redis(socket_timeout=2, socket_connect_timeout=2)
store = RedisStore(client, prefix="myapp:price:v1:", fail_open=False)

@memoize(store=store, ttl=300)
def price(sku: str) -> float:
    return load_price(sku)
```

Keys are hashed by default. `key_format=` can provide an external string key when
other code needs to seed or inspect entries. Values default to pickle; only use it
with a store whose contents are trusted, or provide matching `dumps=` and `loads=`
serializers. Entries are opaque tuples, so custom serializers must round-trip them.

`fail_open=True` (the default) logs a backend error and treats a read as a miss or a
write as a no-op. Set it to `False` when persistence is part of correctness, especially
for [idempotency](../concurrency/leases-and-idempotency.md#idempotency).

`cache_info().currsize` is **None** for this adapter: statistics never scan Redis.
`clear()` scans only the escaped namespace and deletes in bounded batches; clearing a
large namespace can still take time. The store's `size()` also returns None. Python's
`len()` cannot represent an unknown count, so the adapter's compatibility `__len__`
returns zero; use `size()` or `cache_info()` instead.

Cache deadlines stored externally are live Unix wall-clock timestamps, so they follow
clock adjustments rather than retaining an import-time offset. Synchronize clocks on
hosts sharing a store. Local caches continue to use monotonic time; `fake_clock()`
patches both time sources for tests.

!!! note "Synchronous store I/O"
    `memoize` calls `CacheStore` methods synchronously, including from an async wrapper.
    A network-backed store can block the event loop. Use a local cache for a latency-sensitive
    async path, and configure finite timeouts on the Redis client.

## Expiring credentials

The loader returns `(value, expires_in_seconds)`. Callers receive only `value`. A cached
value expires `refresh_margin` seconds early, and concurrent refreshes are coalesced.

```python
from python_wrap import expiring
from python_wrap.testing import fake_clock

calls = []

@expiring(refresh_margin=2)
def access_token():
    calls.append(1)
    return f"token-{len(calls)}", 10

with fake_clock() as clock:
    assert access_token() == access_token() == "token-1"
    clock.advance(8)
    assert access_token() == "token-2"
    access_token.invalidate()      # for example, after a server rejects the credential
    assert access_token() == "token-3"
```

An `async def` loader works with `await access_token()`. Failed loads are not cached.
Lifetimes at or below the margin are not cached. `.cache_info()` reports the underlying
memoize statistics. This is an in-process cache; it does not share OAuth credentials
between workers. `Expiring[P, R]` describes the typed callable and its controls.
Invalidation is a generation barrier: later calls do not join a refresh started before
invalidation. Existing callers can still receive that older refresh's result.

## Serving a last good value

The existing `memoize(ttl=..., stale_ttl=...)` serves a stale result while one background
refresh runs. A failed refresh keeps the stale result until the stale window expires.
Use [fallback](../resilience/fallback.md) for an explicit alternative after protections
are exhausted. These behaviors remain available in 0.3; they are not new backends.

See [memoize](memoize.md), [stores API](../../api/stores.md), and
[credentials API](../../api/credentials.md).
