"""Ownership and cancellation regressions runnable without network services."""

from __future__ import annotations

import asyncio
import threading

import pytest

from python_wrap import (
    CircuitBreaker,
    LocalLease,
    MemoryResultStore,
    PgAdvisoryLock,
    SqlAppLock,
    idempotent,
)
from python_wrap.coordination import AlreadyRunning, Lease, LeaseLost
from python_wrap.quotas import Quota, RedisWindow
from python_wrap.shared import SharedBulkhead
from python_wrap.testing import fake_clock


def test_local_expiry_is_reported_on_exit_and_old_owner_cannot_release_successor():
    backend = LocalLease()
    with fake_clock() as clock:
        lease = Lease("resource", backend=backend, ttl=1, renew=False)
        lease.__enter__()
        clock.advance(2)
        with pytest.raises(LeaseLost):
            lease.__exit__(None, None, None)
        assert backend.acquire("resource", "next", 1)
        assert not backend.release("resource", lease.token)
        assert backend.release("resource", "next")


def test_lease_single_use_and_failed_try_can_be_retried():
    backend = LocalLease()
    assert backend.acquire("resource", "owner", 10)
    lease = Lease("resource", backend=backend, renew=False)
    with pytest.raises(AlreadyRunning):
        lease.__enter__()
    backend.release("resource", "owner")
    with lease, pytest.raises(RuntimeError, match="single-use"):
        lease.__enter__()
    with pytest.raises(RuntimeError, match="single-use"):
        lease.__enter__()


@pytest.mark.parametrize(
    "option,value",
    [
        ("limit", True),
        ("limit", 1.2),
        ("timeout", float("nan")),
        ("timeout", float("inf")),
        ("ttl", 0),
        ("ttl", float("nan")),
    ],
)
def test_lease_validates_bounds(option, value):
    with pytest.raises(ValueError):
        Lease("resource", **{option: value})


@pytest.mark.parametrize(
    "klass,argument",
    [
        (Quota, {"limit": 1.5}),
        (RedisWindow, {"client": object(), "key": "x", "calls": 1.5, "period": 1}),
    ],
)
def test_counters_reject_fractional_capacities(klass, argument):
    with pytest.raises(ValueError):
        klass(**argument)


class SlowAcquire(LocalLease):
    def __init__(self, *, fail=False):
        super().__init__()
        self.started, self.finish = threading.Event(), threading.Event()
        self.fail = fail

    def acquire(self, key, token, ttl, limit=1):
        self.started.set()
        assert self.finish.wait(3)
        acquired = super().acquire(key, token, ttl, limit)
        if self.fail:
            raise RuntimeError("acquire response lost")
        return acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_acquire_settles_and_releases_even_on_second_cancel(fail):
    backend = SlowAcquire(fail=fail)

    async def run():
        async with Lease("resource", backend=backend, renew=False):
            pytest.fail("cancelled work must not start")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(backend.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    backend.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert LocalLease.acquire(backend, "resource", "successor", 10)


class SlowRelease(LocalLease):
    def __init__(self):
        super().__init__()
        self.started, self.finish = threading.Event(), threading.Event()

    def release(self, key, token):
        self.started.set()
        assert self.finish.wait(3)
        return super().release(key, token)


@pytest.mark.asyncio
async def test_cancellation_during_release_is_not_swallowed_or_abandoned():
    backend = SlowRelease()

    async def run():
        async with Lease("resource", backend=backend, renew=False):
            pass

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(backend.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    backend.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.acquire("resource", "successor", 10)


class SlowRenewal(LocalLease):
    def __init__(self):
        super().__init__()
        self.started, self.finish = threading.Event(), threading.Event()
        self.renewing = False
        self.released_during_renewal = False

    def renew(self, key, token, ttl):
        self.renewing = True
        self.started.set()
        assert self.finish.wait(3)
        try:
            return super().renew(key, token, ttl)
        finally:
            self.renewing = False

    def release(self, key, token):
        self.released_during_renewal = self.renewing
        return super().release(key, token)


@pytest.mark.asyncio
async def test_heartbeat_operation_settles_before_sql_style_release():
    backend = SlowRenewal()

    async def run():
        async with Lease("resource", backend=backend, ttl=0.3):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(backend.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    backend.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not backend.released_during_renewal


class CleanupFailure(LocalLease):
    def release(self, key, token):
        super().release(key, token)
        raise RuntimeError("cleanup failed")


def test_cleanup_error_does_not_replace_function_error():
    with (
        pytest.raises(ValueError, match="original"),
        Lease("resource", backend=CleanupFailure(), renew=False),
    ):
        raise ValueError("original")


@pytest.mark.asyncio
async def test_async_cleanup_error_does_not_replace_function_error():
    with pytest.raises(ValueError, match="original"):
        async with Lease("resource", backend=CleanupFailure(), renew=False):
            raise ValueError("original")


def test_idempotency_return_none_does_not_swallow_body_already_running():
    @idempotent(key="body-error", in_progress="return_none")
    def work():
        raise AlreadyRunning("body exception")

    with pytest.raises(AlreadyRunning, match="body exception"):
        work()


@pytest.mark.asyncio
async def test_async_idempotency_return_none_preserves_body_already_running():
    @idempotent(key="body-error-async", in_progress="return_none")
    async def work():
        raise AlreadyRunning("body exception")

    with pytest.raises(AlreadyRunning, match="body exception"):
        await work()


class LostBackend(LocalLease):
    def renew(self, key, token, ttl):
        return False


def test_idempotency_lost_owner_does_not_publish_result():
    store = MemoryResultStore()

    @idempotent(key="lost", store=store, backend=LostBackend())
    def work():
        return 42

    with pytest.raises(LeaseLost):
        work()
    assert store.get("lost") is None


class SlowStore(MemoryResultStore):
    def __init__(self):
        super().__init__()
        self.started, self.finish = threading.Event(), threading.Event()

    def set(self, key, value, ttl):
        self.started.set()
        assert self.finish.wait(3)
        return super().set(key, value, ttl)


@pytest.mark.asyncio
async def test_result_write_settles_before_cancel_releases_idempotency_lease():
    store = SlowStore()
    count = 0

    @idempotent(key="write", store=store, in_progress="return_none")
    async def work():
        nonlocal count
        count += 1
        return 7

    task = asyncio.create_task(work())
    assert await asyncio.to_thread(store.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert await work() is None
    store.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await work() == 7 and count == 1


class Result:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class Connection:
    def __init__(self, *, acquire=True, release=True, fail=None):
        self.acquire_result, self.release_result, self.fail = acquire, release, fail
        self.queries = []
        self.closed = self.invalidated = False
        self.closed_count = self.invalidated_count = 0

    def exec_driver_sql(self, sql, params=None):
        self.queries.append((sql, params))
        if self.fail and self.fail in sql:
            raise RuntimeError("driver failed")
        if "try_advisory" in sql or "sp_getapplock" in sql:
            return Result(self.acquire_result)
        if "advisory_unlock" in sql or "sp_releaseapplock" in sql:
            return Result(self.release_result)
        return Result(1)

    def invalidate(self):
        self.invalidated = True
        self.invalidated_count += 1

    def close(self):
        self.closed = True
        self.closed_count += 1


class Engine:
    def __init__(self, *connections):
        self.connections = iter(connections)

    def connect(self):
        return next(self.connections)


@pytest.mark.parametrize(
    "kind,granted,released", [(PgAdvisoryLock, True, True), (SqlAppLock, 0, 0)]
)
def test_sql_matching_key_and_token_hold_dedicated_session_until_release(kind, granted, released):
    conn = Connection(acquire=granted, release=released)
    backend = kind(Engine(conn))
    assert backend.acquire("key", "owner", 10)
    assert not conn.closed
    assert not backend.renew("wrong-key", "owner", 10)
    assert not backend.release("wrong-key", "owner")
    assert not backend.release("key", "wrong-owner")
    assert backend.renew("key", "owner", 10)
    assert backend.release("key", "owner")
    assert conn.closed_count == 1 and conn.invalidated_count == 0
    assert not backend.release("key", "owner")


@pytest.mark.parametrize("kind,result", [(PgAdvisoryLock, False), (SqlAppLock, -1)])
def test_sql_unsuccessful_unlock_invalidates_session_before_pool_return(kind, result):
    conn = Connection(acquire=True if kind is PgAdvisoryLock else 0, release=result)
    backend = kind(Engine(conn))
    assert backend.acquire("key", "owner", 10)
    assert not backend.release("key", "owner")
    assert conn.invalidated_count == conn.closed_count == 1


@pytest.mark.parametrize("fail", ["try_advisory", "advisory_unlock"])
def test_sql_uncertain_acquire_or_release_physically_discards_session(fail):
    conn = Connection(fail=fail)
    backend = PgAdvisoryLock(Engine(conn))
    if "unlock" in fail:
        assert backend.acquire("key", "owner", 10)
        with pytest.raises(RuntimeError, match="driver failed"):
            backend.release("key", "owner")
    else:
        with pytest.raises(RuntimeError, match="driver failed"):
            backend.acquire("key", "owner", 10)
    assert conn.invalidated_count == conn.closed_count == 1


def test_sql_does_not_reconnect_a_lost_lock_during_renewal(monkeypatch):
    conn = Connection()
    backend = PgAdvisoryLock(Engine(conn))
    assert backend.acquire("key", "owner", 10)
    conn.invalidated = True
    before = len(conn.queries)
    assert not backend.renew("key", "owner", 10)
    assert len(conn.queries) == before
    assert not backend.release("key", "owner")
    assert conn.closed
    monkeypatch.setattr("python_wrap.coordination.os.getpid", lambda: backend._pid + 1)
    with pytest.raises(RuntimeError, match="after fork"):
        backend.acquire("key", "next", 10)


@pytest.mark.asyncio
async def test_shared_bulkhead_copied_task_context_cannot_release_parent_lease():
    bulkhead = SharedBulkhead(1, backend=LocalLease(), name="context", timeout=0)
    await bulkhead.acquire_async()

    async def wrong_task():
        with pytest.raises(RuntimeError, match="acquiring task"):
            await bulkhead.release_async()

    await asyncio.create_task(wrong_task())
    assert bulkhead.active == 1
    await bulkhead.release_async()
    assert bulkhead.active == 0


class SlowBreakerBackend:
    def __init__(self, *, fail_settle=False):
        self.started, self.finish = threading.Event(), threading.Event()
        self.operations = []
        self.fail_settle = fail_settle

    def operate(self, breaker, operation, generation=0, trial=False):
        self.operations.append(operation)
        if operation == "admit":
            self.started.set()
            assert self.finish.wait(3)
        elif self.fail_settle and operation == "failure":
            raise RuntimeError("backend down")
        return {
            "old": "half_open",
            "state": "half_open",
            "admitted": 1,
            "generation": 1,
            "trial": 1,
        }


@pytest.mark.asyncio
async def test_cancelled_shared_breaker_admission_releases_late_trial():
    backend = SlowBreakerBackend()
    breaker = CircuitBreaker(name="slow-admit", backend=backend)

    async def body():
        pytest.fail("cancelled caller must not execute body")

    task = asyncio.create_task(breaker.call(body))
    assert await asyncio.to_thread(backend.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    backend.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.operations == ["admit", "neutral"]


@pytest.mark.asyncio
async def test_shared_breaker_bookkeeping_failure_preserves_original_exception():
    backend = SlowBreakerBackend(fail_settle=True)
    backend.finish.set()
    breaker = CircuitBreaker(name="bookkeeping", backend=backend)

    async def body():
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        await breaker.call(body)


@pytest.mark.asyncio
async def test_shared_manual_async_acquisition_rejects_before_taking_any_lease():
    from python_wrap import Bulkhead

    backend = LocalLease()
    bulkhead = Bulkhead(1, backend=backend, name="manual-api")
    with pytest.raises(RuntimeError, match="async with"):
        await bulkhead.acquire_async()
    assert backend.acquire("manual-api", "probe", 10)
    assert backend.release("manual-api", "probe")
    assert bulkhead.active == 0
    async with bulkhead:
        assert bulkhead.active == 1
    assert bulkhead.active == 0
