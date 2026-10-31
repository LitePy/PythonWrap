# Migrating a hand-rolled lock helper

Many services carry a small module that wraps Redis `SET NX EX` and `DEL` behind calls such as
`with named_lock(name, suffix):`, `acquire_lock(name)` and `release_lock(name)`.
`python_wrap.compat` offers the same call shapes on top of owner-checked [leases](leases-and-idempotency.md),
so call sites keep their code and only the import line changes.

```python
from python_wrap import LocalLease
from python_wrap.compat import acquire_lock, configure_locks, release_lock, named_lock

configure_locks(LocalLease())  # in production: RedisLock(client)

with named_lock("nightly-import", "tenant-7") as lease:
    assert lease.key == "lock:nightly-import:tenant-7"

if acquire_lock("report"):
    release_lock("report")
```

## Context manager

`named_lock(name, suffix=None, *, backend=None, ttl=600, wait=False, timeout=None, prefix="lock:", skip=False)`
holds the key `prefix + name` (plus `":" + suffix`). The lease renews itself while the block runs, so `ttl` only
bounds how long a crashed worker keeps the lock.

When the lock is held elsewhere the block is not entered and `AlreadyRunning` is raised. To skip quietly instead:

```python
from python_wrap.compat import named_lock


def run(tenant: str) -> str:
    with named_lock("sync", tenant, skip=True) as acquired:
        if not acquired:
            return "already running"
        return "done"


assert run("t1") == "done"
```

`wait=True, timeout=30` waits for the lock for up to 30 seconds. If the lease is lost while the block runs
(for example the backend was unreachable past the `ttl`), `LeaseLost` is raised on leaving the block.

## Acquire and release functions

`acquire_lock(name) -> bool` and `release_lock(name) -> bool` use a process-wide `RunLocks` object. Create
your own with `RunLocks(backend, ttl=..., prefix=...)` for different settings; it also offers `held(name)` and
`renew(name)`. These locks are not auto-renewed, like the `SET NX EX` pattern they replace: pick a `ttl` longer than
the work, or call `renew`.

## One behavioural improvement

A helper whose release is a bare `DEL` can free a lock that expired and was since taken by another worker. Here every
acquisition carries a unique token that the process remembers, and `release` only removes the lock if that token still
owns it. A release by a worker that never held, or no longer holds, the lock returns `False` and leaves the lock alone.
Releases are therefore safe to call from `finally` blocks.

## Choosing the backend

`configure_locks(backend)` sets the default for `named_lock`, `acquire_lock` and `release_lock`. Without it,
locks are in-process only (correct for tests, not for multiple workers). Use `RedisLock(client)` for several hosts.
For tokens in a forked child: a child process does not inherit the parent's `RunLocks` tokens, so acquire in the process
that releases.
