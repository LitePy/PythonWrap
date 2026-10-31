# Leases, single-instance work, and idempotency

Use a lease to exclude overlapping work, and idempotency to remember completed results.
They address different parts of repeated task delivery.

## One call at a time

```python
from python_wrap import LocalLease, single_instance

backend = LocalLease()

@single_instance(key="nightly-import", backend=backend, ttl=60, renew=False)
def import_once_at_a_time() -> str:
    return "imported"

assert import_once_at_a_time() == "imported"
```

The default backend is in-process. `wait=False` raises `AlreadyRunning` immediately.
`wait=True` waits for the lease, optionally bounded by `timeout=`. An explicit `key=`
string coordinates all calls under that key; a callable can derive the key from arguments.
When omitted, a key is derived from the function and its bound arguments; use an explicit
key for service objects or arguments that cannot be serialized deterministically.

`Lease` is also a sync/async context manager. Each context is single-use, cannot be
entered concurrently, and must not cross a fork. Backend I/O in async lease operations
runs in a worker thread. Cancellation cleanup waits for already-started client I/O to
settle and releases an acquired token; clients therefore need finite I/O timeouts. By default a heartbeat renews
the lease while the function runs. A lost lease raises `LeaseLost` on otherwise successful
completion; an existing function exception or cancellation is preserved.

## Redis and SQL backends

<!-- skip -->
```python
import redis
from python_wrap import RedisLock, single_instance

client = redis.Redis(socket_timeout=2, socket_connect_timeout=2)
leases = RedisLock(client, prefix="myapp:leases:")

@single_instance(key="fixtures-import", backend=leases, ttl=900, renew=True)
def import_fixtures():
    return run_import()
```

Redis acquisition, renewal and release check a unique owner token atomically, using
Redis's clock. An expired owner cannot unlock its successor. A `RedisSemaphore` uses the
same lease protocol with a capacity greater than one; [Bulkhead](shared-limits.md#shared-bulkheads)
exposes that behavior for function calls.

`PgAdvisoryLock(engine)` and `SqlAppLock(engine)` accept a SQLAlchemy engine and hold a
dedicated session connection while the lease is held. SQL session locks last for the
session rather than a wall-clock TTL. Capacity must be one. Create engines and SQL
lock backends after fork; inherited SQL backends reject use. Use client/database
timeouts. Unlock failures invalidate the dedicated connection rather than returning a
potentially locked session to its pool. These adapters need your application's SQLAlchemy
installation and database driver; python-wrap does not install them.

!!! warning "A lease is not a transaction"
    Pausing longer than a lease TTL, losing connectivity, or crashing can let another
    worker proceed while old external work is still running. Renewal reduces that risk
    but cannot provide exactly-once side effects. Use database idempotency keys,
    transactional outboxes, or downstream fencing when writes require that guarantee.

## Idempotency

```python
from python_wrap import idempotent

calls = []

@idempotent(key=lambda file_id: f"import:{file_id}", ttl="1d")
def import_file(file_id: str):
    calls.append(file_id)
    return {"imported": file_id}

assert import_file("a") == import_file("a") == {"imported": "a"}
assert calls == ["a"]
```

A successful result, including None, is retained for `ttl`. A failure is not cached and
releases the lease. Duplicates during execution follow `in_progress=`: `"raise"`,
`"wait"`, or `"return_none"`. `lease_ttl` controls in-progress ownership independently
of result retention; renewal is automatic. For waits, `timeout=` bounds acquisition.

Cross-process behavior needs **both** shared result storage and shared lease ownership.
`RedisStore(fail_open=False)` automatically supplies a matching `RedisLock` when
`backend=` is omitted. Other shared stores need an explicit shared lease backend;
you can also pass one to choose a separate Redis lease namespace. A distributed lease
with the default local result store is rejected. Backend write failures must propagate,
so fail-open result stores are rejected.

<!-- skip -->
```python
from python_wrap import RedisLock, RedisStore, idempotent

@idempotent(key=lambda event: f"event:{event['id']}", ttl="1d",
            store=RedisStore(client, "myapp:idempotency:", fail_open=False),
            backend=RedisLock(client, "myapp:idempotency-leases:"),
            in_progress="wait", timeout=30)
def handle_event(event):
    return apply_event(event)
```

Ownership is checked and extended immediately before publishing a result. A generic
result store cannot atomically fence a write to current lease ownership: if a client
write stalls beyond lease expiry, an old worker may publish a stale result. Client I/O
timeouts should be shorter than the lease. A crash after the external effect but before
the result is stored can replay that effect. Use an external transaction, fencing protocol,
or downstream idempotency key when either gap matters.

See [coordination API](../../api/coordination.md), [idempotency API](../../api/idempotency.md),
and [fork behavior](../../project/migration-0.3.md#forked-workers).
