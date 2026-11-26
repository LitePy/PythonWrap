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
from python_wrap.stores import signed_pickle_codec

client = redis.Redis(socket_timeout=2, socket_connect_timeout=2)
dumps, loads = signed_pickle_codec(b"replace-with-a-secret-at-least-16-bytes")
store = RedisStore(client, prefix="myapp:price:", version=1,
                   dumps=dumps, loads=loads, fail_open=False)

@memoize(store=store, ttl=300)
def price(sku: str) -> float:
    return load_price(sku)
```

Keys are hashed by default. `key_format=` can provide an external string key when
other code needs to seed or inspect entries. `version=` isolates a new deployment's
entries. Implicit pickle is deprecated; pass `signed_pickle_codec(secret)` or
`json_codec()` from `python_wrap.stores`. `configure(cache_codec=(dumps, loads))`
sets the default for both sync and async Redis stores. Entries are opaque tuples,
so custom serializers must round-trip them. Invalid signatures are misses and emit
`cache.rejected` before anything is unpickled.

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

## Many processes: stampedes, tags and jitter

Several worker processes sharing one `RedisStore` cause three problems. They miss at the
same moment and all compute the same value. A change to one customer's data leaves old
entries in place until their ttl ends. Entries written together also expire together.
The options below address each one.

### One computation per miss

Pass a lease backend as `lock=`. Use `RedisLock(client)` with a synchronous client. For
`amemoize` with a `redis.asyncio` client, use `AsyncRedisLock(client)`. On a miss, one
process takes a lease keyed by the cache key and computes. The others wait up to
`lock_wait` seconds, read the store again, and compute only if the entry is still absent.
`lock_ttl` is the lease lifetime. It is renewed while the function runs. `lock=` without
`store=` raises `ValueError`.

With `stale_ttl`, a refresh takes the same lease but does not wait. One process refreshes
and the others keep serving the stale value.

### Tags

`tags=` takes a callable. It receives the function's arguments and returns tag names for
the entry, for example `tags=lambda customer_id: [f"customer:{customer_id}"]`.
`invalidate_tags("customer:7")` makes every entry written with that tag miss on its next
read.

Each tag has a version token. An entry records the tokens it was written under, and a
read with a different token is a miss. Invalidating a tag is therefore one write, and no
scan is needed. With a `RedisStore`, tokens are kept in Redis under keys
`python-wrap:tag:<hash>`, so other processes see the invalidation. `invalidate_tags`
writes through every sync Redis client used by a tagged `memoize` in the process. Pass
`client=` to name one explicitly. For `redis.asyncio` clients, use
`await ainvalidate_tags(...)`.

Two limits apply. Tags are read before the function runs, so an invalidation during the
call leaves the new entry stale. Each tag costs one store read per hit. If the tag version
cannot be read, the entry counts as a miss.

### Jitter

`ttl_jitter=0.1` shortens each entry's ttl by a random share of up to 10 percent, so
entries written together do not expire together. The value must be in `[0, 1)`.

<!-- skip -->
```python
from redis import Redis
from python_wrap import RedisLock, RedisStore, invalidate_tags, memoize

client = Redis()

@memoize(
    store=RedisStore(client, "orders:", fail_open=False),
    ttl=300,
    ttl_jitter=0.1,
    lock=RedisLock(client),
    tags=lambda customer_id: [f"customer:{customer_id}"],
)
def open_orders(customer_id: int) -> list[dict]:
    return load_orders(customer_id)

invalidate_tags("customer:7")  # after customer 7's orders change
```

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
