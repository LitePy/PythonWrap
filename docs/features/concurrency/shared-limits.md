# Shared limits, quotas, and circuit state

Default limiters, breakers and bulkheads are in-process. Optional Redis backends extend
coordination across workers; they take a client created by your application.

## A run quota

```python
from python_wrap import Quota, QuotaExhausted

quota = Quota(2, name="import")
assert quota.take() == 1
assert quota.take() == 0
try:
    quota.take()
except QuotaExhausted:
    pass
else:
    raise AssertionError("quota should be exhausted")
```

`.take(amount)` is atomic; `.remaining` reports allowance and `.reset()` explicitly
starts over. To share a quota use `client=`, a unique Redis `key=` for the run, and an
optional `ttl=` for retention. A per-run quota is different from a rolling rate limit.

## Redis sliding windows

<!-- skip -->
```python
from python_wrap import RateLimiter, RedisWindow

window = RedisWindow(client, key="myapp:vald-window", calls=30, period=60)
limiter = RateLimiter(backend=window, name="vald", block=True)

@limiter
def fetch_trials():
    return request_trials()
```

The admission decision is one atomic Lua operation using Redis's clock. Calls in every
process with the same namespace and configuration share the limit. `shared_rate_limit`
is a lighter decorator around a `RedisWindow`. Async wrappers offload backend calls;
set finite Redis I/O timeouts. Backend errors propagate rather than treating an unknown
admission decision as success. `reset()`/`clear()` is an administrative/test operation.

## Shared circuit breakers

<!-- skip -->
```python
from python_wrap import CircuitBreaker, RedisBreakerState

breaker = CircuitBreaker(name="catalog", failure_threshold=5, window=60,
                         recovery_timeout=30, half_open_max_calls=1,
                         on=(ConnectionError, TimeoutError),
                         backend=RedisBreakerState(client))

@breaker
def fetch_catalog():
    return request_catalog()
```

All participants must use the same name and configuration. `window=` counts failures
within a time window; without it the existing consecutive-failure policy remains.
`on=` and `exclude=` keep validation or other non-transient errors from tripping the
breaker. Open circuits raise `CircuitOpenError`, which retry never retries in 0.3.

Redis tracks state, failure counts and half-open reservations atomically. Trial
reservations have bounded lifetimes so a crashed probing worker cannot hold a slot
forever. Late results from an older breaker generation are ignored. `.force_open()`
and `.reset()` support maintenance and tests. See [circuit breaker](../resilience/circuit-breaker.md).

## Shared bulkheads

<!-- skip -->
```python
from python_wrap import Bulkhead, RedisSemaphore

capacity = Bulkhead(5, name="myapp:openai", backend=RedisSemaphore(client),
                    ttl=60, max_waiting=20, timeout=2)

@capacity
async def call_model():
    return await request_model()
```

The lease-backed capacity is shared; `max_waiting` bounds the queue in **each process**.
`active` and `waiting` report local calls, not a global monitor. Sync and async context
managers work too. For shared async use `async with capacity` so release can await
backend I/O. Lease expiry has the same [limits](leases-and-idempotency.md#redis-and-sql-backends)
as a distributed lock; long external work must tolerate a lost lease.

See [quotas API](../../api/quotas.md), [shared backends API](../../api/shared.md),
and [bulkhead API](../../api/bulkhead.md).
