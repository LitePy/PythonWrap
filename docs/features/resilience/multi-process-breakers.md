# Circuit breakers across processes

A `CircuitBreaker` keeps its state in memory. Under gunicorn, uvicorn with several workers, or Celery's prefork pool,
every worker process has its own copy, and each one has to see `failure_threshold` failures by itself before it opens.

## When per-process state is fine

- The dependency is slow or down for minutes, not seconds: every worker trips soon anyway.
- Traffic per worker is high, so each reaches the threshold quickly.
- You have few workers, and a few extra failing calls while they each learn about the outage are cheap.

Keep the default in these cases. It has no I/O on the call path and nothing to deploy.

## One host: SQLiteBreakerState

When the workers share a machine and you want one worker's failures to protect the others, share the state in a local
file. No Redis is needed.

```python
import os
import tempfile

from python_wrap import CircuitBreaker, CircuitOpenError
from python_wrap.local_state import SQLiteBreakerState

with tempfile.TemporaryDirectory() as tmp:
    # Use a fixed, writable path in production, e.g. /run/myapp/breakers.db
    state = SQLiteBreakerState(os.path.join(tmp, "breakers.db"))

    worker_1 = CircuitBreaker(name="vendor-api", backend=state, failure_threshold=2)
    worker_2 = CircuitBreaker(name="vendor-api", backend=state, failure_threshold=2)

    for _ in range(2):
        try:
            worker_1.call(lambda: 1 / 0)
        except ZeroDivisionError:
            pass

    try:
        worker_2.call(lambda: "never runs")
    except CircuitOpenError:
        pass
    else:
        raise AssertionError("worker_2 should be rejected")
    state.close()
```

Behaviour matches `RedisBreakerState`: generation fencing so late results from before a reset or re-open are ignored,
a cap on concurrent half-open trial calls, a trial deadline of `max(1s, recovery_timeout)` so a crashed probe cannot
hold the slot, an optional failure `window`, and the totals in `.stats`.

Notes:

- Create the object in each process (or let it reconnect after fork: connections are reopened automatically when the
  process id changes). Each thread uses its own connection.
- Every operation is one short `BEGIN IMMEDIATE` transaction in WAL mode. If the database stays locked longer than
  `busy_timeout` (default 5 seconds) the error propagates, as it does for Redis; it is never treated as a healthy
  dependency.
- The directory must exist and be writable by every worker. Do not use a network file system.
- Time is the host's wall clock, which all processes share.

## Many hosts: RedisBreakerState

Use `RedisBreakerState(client)` when instances run on different machines (see
[shared limits](../concurrency/shared-limits.md)). It has the same contract and uses the Redis server's clock.

## Celery prefork

Each prefork child is a separate process, so by default each has its own breaker and one task failing does not warn
its siblings. Give the breaker a shared backend and a stable name per vendor, defined once at module level:

<!-- skip -->
```python
from python_wrap import CircuitBreaker
from python_wrap.local_state import SQLiteBreakerState

vendor_breaker = CircuitBreaker(
    name="vendor-api",  # one stable name per dependency, not per task
    backend=SQLiteBreakerState("/var/run/myapp/breakers.db"),
    failure_threshold=5,
    recovery_timeout=30,
)


@app.task
def sync_vendor():
    return vendor_breaker.call(fetch_from_vendor)
```

Use the same name and settings in every process; a shared breaker must have an explicit `name`. When workers run on
several hosts, swap in `RedisBreakerState`.
