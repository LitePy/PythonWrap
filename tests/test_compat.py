"""Tests for the lock-helper migration shim."""

from __future__ import annotations

import pytest

from python_wrap import LocalLease, RedisLock
from python_wrap.compat import (
    RunLocks,
    acquire_lock,
    configure_locks,
    named_lock,
    release_lock,
)
from python_wrap.coordination import _LEASE, AlreadyRunning, LeaseLost


class FakeRedis:
    """Implements the one Lua script RedisLock sends, with a controllable clock."""

    def __init__(self):
        self.now_ms = 1_000_000
        self.sets: dict[str, dict[str, int]] = {}

    def eval(self, script, numkeys, key, op, token, ttl_ms, limit):
        assert script == _LEASE and numkeys == 1
        entries = self.sets.setdefault(key, {})
        for tok in [t for t, exp in entries.items() if exp <= self.now_ms]:
            del entries[tok]
        if op == "release":
            return 1 if entries.pop(token, None) is not None else 0
        if op == "renew":
            if token not in entries:
                return 0
        elif token not in entries and len(entries) >= int(limit):
            return 0
        entries[token] = self.now_ms + int(ttl_ms)
        return 1


@pytest.fixture(autouse=True)
def _reset_default():
    configure_locks(None)
    yield
    configure_locks(None)


@pytest.fixture(params=["local", "redis"])
def backend(request):
    if request.param == "local":
        return LocalLease()
    return RedisLock(FakeRedis(), prefix="app:")


def test_named_lock_excludes_and_releases(backend):
    with named_lock("job", "a", backend=backend):
        with pytest.raises(AlreadyRunning), named_lock("job", "a", backend=backend):
            pytest.fail("must not enter")
        with named_lock("job", "b", backend=backend):
            pass  # a different suffix is a different lock
    with named_lock("job", "a", backend=backend):
        pass


def test_named_lock_key_shape():
    with named_lock("job", "tenant-1", backend=LocalLease(), prefix="x:") as lease:
        assert lease.key == "x:job:tenant-1"
    with named_lock("job", backend=LocalLease()) as lease:
        assert lease.key == "lock:job"


def test_skip_yields_false_instead_of_raising(backend):
    with (
        named_lock("job", backend=backend),
        named_lock("job", backend=backend, skip=True) as acquired,
    ):
        assert acquired is False
    with named_lock("job", backend=backend, skip=True) as acquired:
        assert acquired


def test_released_when_body_raises(backend):
    with pytest.raises(ValueError), named_lock("job", backend=backend):
        raise ValueError("boom")
    with named_lock("job", backend=backend):
        pass


def test_wait_with_timeout_gives_up():
    backend = LocalLease()
    with (
        named_lock("job", backend=backend),
        pytest.raises(AlreadyRunning),
        named_lock("job", backend=backend, wait=True, timeout=0.1),
    ):
        pass


def test_lease_lost_is_reported(fake_clock):
    backend = LocalLease()
    with pytest.raises(LeaseLost), named_lock("job", backend=backend, ttl=10):
        fake_clock.advance(11)


def test_configured_default_backend_is_used():
    backend = LocalLease()
    configure_locks(backend)
    with named_lock("job"):
        assert not backend.acquire("lock:job", "other", 5)
    assert acquire_lock("report")
    assert not backend.acquire("lock:report", "other", 5)
    assert release_lock("report")
    assert backend.acquire("lock:report", "other", 5)


def test_run_locks_acquire_release_held(backend):
    locks = RunLocks(backend, ttl=30)
    assert not locks.held("x")
    assert locks.acquire("x")
    assert locks.held("x")
    assert not locks.acquire("x")
    assert locks.release("x")
    assert not locks.held("x")
    assert not locks.release("x")
    assert locks.acquire("x")


def test_second_worker_cannot_release_or_take_over(backend):
    worker_a, worker_b = RunLocks(backend), RunLocks(backend)
    assert worker_a.acquire("x")
    assert not worker_b.acquire("x")
    assert not worker_b.release("x")  # a plain DEL helper would free A's lock here
    assert not worker_b.acquire("x")
    assert worker_a.release("x")
    assert worker_b.acquire("x")


def test_expired_owner_cannot_release_successor_local(fake_clock):
    backend = LocalLease()
    a, b = RunLocks(backend, ttl=10), RunLocks(backend, ttl=10)
    assert a.acquire("x")
    fake_clock.advance(11)
    assert not a.held("x")
    assert b.acquire("x")
    assert not a.release("x")
    assert not a.acquire("x")  # b still owns it


def test_expired_owner_cannot_release_successor_redis():
    client = FakeRedis()
    backend = RedisLock(client)
    a, b = RunLocks(backend, ttl=10), RunLocks(backend, ttl=10)
    assert a.acquire("x")
    client.now_ms += 11_000
    assert b.acquire("x")
    assert not a.release("x")
    assert not b.acquire("x")
    assert b.release("x")


def test_renew_extends_and_fails_after_takeover(fake_clock):
    backend = LocalLease()
    a, b = RunLocks(backend, ttl=10), RunLocks(backend, ttl=10)
    assert a.acquire("x")
    fake_clock.advance(8)
    assert a.renew("x")
    fake_clock.advance(8)
    assert a.held("x") and not b.acquire("x")
    fake_clock.advance(5)
    assert b.acquire("x")
    assert not a.renew("x")
    assert not a.renew("never")


def test_empty_name_rejected():
    with pytest.raises(ValueError):
        RunLocks(LocalLease()).acquire("")
