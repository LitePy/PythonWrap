"""Exercise actual Lua scripts against a disposable Redis, without a Python redis dependency."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from python_wrap import (
    CircuitBreaker,
    CircuitOpenError,
    Quota,
    QuotaExhausted,
    RateLimiter,
    RedisBreakerState,
    RedisLock,
    RedisStore,
    RedisWindow,
    idempotent,
    memoize,
)


class Client:
    def __init__(self, path):
        self.path = path

    def execute(self, *args):
        values = [value if isinstance(value, bytes) else str(value).encode() for value in args]
        payload = (
            b"*"
            + str(len(values)).encode()
            + b"\r\n"
            + b"".join(
                b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n" for value in values
            )
        )
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(self.path)
            conn.sendall(payload)
            stream = conn.makefile("rb")

            def read():
                line = stream.readline()
                if not line:
                    raise RuntimeError("Redis disconnected")
                kind, body = line[:1], line[1:-2]
                if kind == b"-":
                    raise RuntimeError(body.decode())
                if kind == b":":
                    return int(body)
                if kind == b"+":
                    return body
                size = int(body)
                if size < 0:
                    return None
                if kind == b"*":
                    return [read() for _ in range(size)]
                value = stream.read(size)
                assert stream.read(2) == b"\r\n"
                return value

            return read()

    def get(self, key):
        return self.execute("GET", key)

    def set(self, key, value, **kw):
        args = ["SET", key, value]
        for k, v in kw.items():
            args.extend([k.upper(), v])
        return self.execute(*args)

    def delete(self, *keys):
        return self.execute("DEL", *keys)

    def unlink(self, *keys):
        return self.execute("UNLINK", *keys)

    def eval(self, script, count, *args):
        return self.execute("EVAL", script, count, *args)

    def scan_iter(self, **kw):
        cursor = 0
        while True:
            cursor, keys = self.execute(
                "SCAN", cursor, "MATCH", kw["match"], "COUNT", kw.get("count", 200)
            )
            yield from keys
            if int(cursor) == 0:
                break


@pytest.fixture(scope="module")
def redis_client():
    executable = shutil.which("redis-server")
    if executable is None or not hasattr(socket, "AF_UNIX"):
        if os.environ.get("PW_REQUIRE_REDIS") == "1":
            pytest.fail("redis-server and Unix sockets required")
        pytest.skip("redis-server and Unix sockets required")
    # Short path for macOS's AF_UNIX length limit.
    with tempfile.TemporaryDirectory(prefix="pw-redis-", dir="/tmp") as root:
        path = str(Path(root) / "r.sock")
        proc = subprocess.Popen(
            [executable, "--port", "0", "--unixsocket", path, "--save", "", "--appendonly", "no"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            client = Client(path)
            for _ in range(100):
                if Path(path).exists():
                    break
                if proc.poll() is not None:
                    log = proc.stdout.read().decode()
                    if (
                        "Operation not permitted" in log
                        and os.environ.get("PW_REQUIRE_REDIS") != "1"
                    ):
                        pytest.skip("sandbox prohibits Redis socket binding")
                    pytest.fail(log)
                time.sleep(0.01)
            assert client.execute("PING") == b"PONG"
            yield client
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_store_roundtrip_ttl_unknown_size_and_escaped_clear(redis_client):
    store = RedisStore(redis_client, "cache[*]?:", fail_open=False)
    other = RedisStore(redis_client, "cacheABC:", fail_open=False)
    store.set(("a",), {"value": 3}, 0.04)
    other.set(("a",), "keep", 60)
    assert store.get(("a",)) == {"value": 3}
    assert store.size() is None
    store.clear()
    assert store.get(("a",)) is None
    assert other.get(("a",)) == "keep"
    store.set("expires", 1, 0.02)
    time.sleep(0.03)
    assert store.get("expires") is None

    @memoize(store=store, ttl=60)
    def cached(value):
        return value * 2

    assert cached(2) == cached(2) == 4
    assert cached.cache_info().currsize is None


def test_redis_lease_atomicity_expiry_and_old_owner_cannot_unlock(redis_client):
    first = RedisLock(redis_client, "leases:")
    second = RedisLock(redis_client, "leases:")
    assert first.acquire("key", "old", 0.03)
    assert not second.acquire("key", "new", 0.03)
    time.sleep(0.04)
    assert second.acquire("key", "new", 1)
    assert not first.release("key", "old")
    assert second.renew("key", "new", 1)
    assert not first.acquire("key", "another", 1)
    assert second.release("key", "new")

    def acquire(index):
        return first.acquire("capacity", str(index), 10, 3)

    with ThreadPoolExecutor(12) as pool:
        assert sum(pool.map(acquire, range(30))) == 3


def test_redis_window_and_quota_are_shared_and_atomic(redis_client):
    a = RedisWindow(redis_client, "window", calls=2, period=0.1)
    b = RedisWindow(redis_client, "window", calls=2, period=0.1)
    assert a.acquire() == b.acquire() == 0
    assert a.acquire() > 0
    a.clear()
    limiter = RateLimiter(backend=a)
    assert limiter.try_acquire() and limiter.try_acquire()
    assert not limiter.try_acquire()
    quota = Quota(5, client=redis_client, key="quota")

    def take(_):
        try:
            return quota.take() >= 0
        except QuotaExhausted:
            return False

    with ThreadPoolExecutor(10) as pool:
        assert sum(pool.map(take, range(20))) == 5
    assert Quota(5, client=redis_client, key="quota").remaining == 0


def test_shared_breaker_rejects_other_instances_and_resets(redis_client):
    state = RedisBreakerState(redis_client)
    a = CircuitBreaker(name="remote", backend=state, failure_threshold=2, recovery_timeout=0.04)
    b = CircuitBreaker(name="remote", backend=state, failure_threshold=2, recovery_timeout=0.04)

    def bad():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        a.call(bad)
    with pytest.raises(ConnectionError):
        b.call(bad)
    with pytest.raises(CircuitOpenError):
        a.call(lambda: 1)
    assert b.state.value == "open"
    time.sleep(0.05)
    assert b.call(lambda: 7) == 7
    assert a.state.value == "closed"
    a.force_open()
    with pytest.raises(CircuitOpenError):
        b.call(lambda: 1)
    a.reset()
    assert b.call(lambda: 1) == 1


def test_redis_idempotency_shared_result_and_lease(redis_client):
    store = RedisStore(redis_client, "idem:", fail_open=False)
    backend = RedisLock(redis_client, "idem-locks:")
    calls = []

    def body():
        calls.append(1)
        return {"done": True}

    a = idempotent(key="same", store=store, backend=backend)(body)
    b = idempotent(key="same", store=store, backend=backend)(body)
    assert a() == b() == {"done": True}
    assert len(calls) == 1


def test_redis_semaphore_release_never_shortens_other_owners_ttl(redis_client):
    backend = RedisLock(redis_client, "unequal-ttl:")
    assert backend.acquire("resource", "long", 1, 2)
    assert backend.acquire("resource", "short", 0.02, 2)
    assert backend.renew("resource", "short", 0.03)
    time.sleep(0.04)
    assert not backend.acquire("resource", "blocked", 1, 1)
    assert backend.release("resource", "long")


def test_redis_reacquire_same_token_is_idempotent(redis_client):
    backend = RedisLock(redis_client, "same-token:")
    assert backend.acquire("resource", "same", 1)
    assert backend.acquire("resource", "same", 1)
    assert not backend.acquire("resource", "other", 1)
    assert backend.release("resource", "same")


def test_redis_half_open_capacity_generation_and_reset_stats(redis_client):
    state = RedisBreakerState(redis_client)
    breaker = CircuitBreaker(
        name="trials",
        backend=state,
        failure_threshold=1,
        recovery_timeout=0.03,
        half_open_max_calls=1,
    )
    breaker.force_open()
    time.sleep(0.04)
    old_generation, trial = breaker._acquire()
    assert trial
    with pytest.raises(CircuitOpenError):
        breaker._acquire()
    # Reset fences out the old trial's late failure.
    breaker.reset()
    breaker._failure(old_generation, trial)
    assert breaker.state.value == "closed"
    assert breaker.stats.failures == 0 and breaker.stats.rejected == 0
    assert breaker.call(lambda: 7) == 7
    assert breaker.stats.successes == 1


def test_redis_failure_window_ignores_old_failures(redis_client):
    backend = RedisBreakerState(redis_client)
    breaker = CircuitBreaker(
        name="windowed", backend=backend, failure_threshold=2, recovery_timeout=1, window=0.03
    )

    def fail():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        breaker.call(fail)
    time.sleep(0.04)
    with pytest.raises(ConnectionError):
        breaker.call(fail)
    assert breaker.state.value == "closed"
    with pytest.raises(ConnectionError):
        breaker.call(fail)
    assert breaker.state.value == "open"


def test_redis_cache_store_infers_shared_idempotency_lock(redis_client):
    calls = []

    def body():
        calls.append(1)
        return 11

    a = idempotent(key="auto", store=RedisStore(redis_client, "auto-idem:", fail_open=False))(body)
    b = idempotent(key="auto", store=RedisStore(redis_client, "auto-idem:", fail_open=False))(body)
    assert a() == b() == 11 and calls == [1]
