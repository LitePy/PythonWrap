"""Tests for python_wrap.adaptive."""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from python_wrap import events
from python_wrap.adaptive import AdaptiveLimiter, LoadShed, PerKeyLimiter, per_key
from python_wrap.bulkhead import Bulkhead
from python_wrap.concurrency import RateLimiter
from python_wrap.errors import BulkheadFull, RateLimitExceeded
from python_wrap.testing import FakeClock


def finish_ok(limiter: AdaptiveLimiter, n: int = 1) -> None:
    for _ in range(n):
        limiter.acquire().release()


def test_validation() -> None:
    with pytest.raises(ValueError):
        AdaptiveLimiter(min_limit=0)
    with pytest.raises(ValueError):
        AdaptiveLimiter(initial=500)
    with pytest.raises(ValueError):
        AdaptiveLimiter(backoff_ratio=1.5)
    with pytest.raises(ValueError):
        AdaptiveLimiter(increase=0)
    with pytest.raises(ValueError):
        AdaptiveLimiter(target_latency=0)
    with pytest.raises(ValueError):
        AdaptiveLimiter().acquire(priority="urgent")  # type: ignore[arg-type]


def test_additive_increase_after_a_window_of_successes() -> None:
    limiter = AdaptiveLimiter(initial=4, max_limit=10)
    finish_ok(limiter, 3)
    assert limiter.limit == 4  # window is the current limit: 4 samples
    finish_ok(limiter, 1)
    assert limiter.limit == 5
    finish_ok(limiter, 5)
    assert limiter.limit == 6
    assert limiter.stats.increases == 2


def test_explicit_window_and_increase() -> None:
    limiter = AdaptiveLimiter(initial=2, window=2, increase=3, max_limit=20)
    finish_ok(limiter, 2)
    assert limiter.limit == 5


def test_increase_is_capped_at_max_limit() -> None:
    limiter = AdaptiveLimiter(initial=2, max_limit=2, window=1)
    finish_ok(limiter, 10)
    assert limiter.limit == 2


def test_multiplicative_decrease_on_matching_error() -> None:
    limiter = AdaptiveLimiter(initial=20, backoff_ratio=0.5, min_limit=3)
    limiter.acquire().release(error=TimeoutError())
    assert limiter.limit == 10
    limiter.acquire().release(error=ConnectionResetError())
    assert limiter.limit == 5
    limiter.acquire().release(error=TimeoutError())
    limiter.acquire().release(error=TimeoutError())
    assert limiter.limit == 3  # floor
    assert limiter.stats.drops == 4
    assert limiter.stats.decreases == 3


def test_backoff_resets_the_success_window() -> None:
    limiter = AdaptiveLimiter(initial=4, backoff_ratio=0.5)
    finish_ok(limiter, 3)
    limiter.acquire().release(error=TimeoutError())
    assert limiter.limit == 2
    finish_ok(limiter, 1)
    assert limiter.limit == 2  # the earlier samples no longer count


def test_unrelated_errors_do_not_change_the_limit() -> None:
    limiter = AdaptiveLimiter(initial=4)
    with pytest.raises(ValueError), limiter.acquire():
        raise ValueError("bad input")
    assert limiter.limit == 4
    assert limiter.in_flight == 0
    assert limiter.stats.drops == 0 and limiter.stats.successes == 0


def test_custom_on_types() -> None:
    limiter = AdaptiveLimiter(initial=10, on=(KeyError,), backoff_ratio=0.5)
    limiter.acquire().release(error=TimeoutError())
    assert limiter.limit == 10
    limiter.acquire().release(error=KeyError("x"))
    assert limiter.limit == 5


def test_inner_bulkhead_rejection_counts_as_overload() -> None:
    limiter = AdaptiveLimiter(initial=10, backoff_ratio=0.5)
    inner = Bulkhead(1, max_waiting=0)

    @limiter
    def call() -> None:
        with inner, inner:
            pass

    with pytest.raises(BulkheadFull):
        call()
    assert limiter.limit == 5


def test_ignore_release_does_not_count() -> None:
    limiter = AdaptiveLimiter(initial=2, window=1)
    limiter.acquire().release(ignore=True)
    assert limiter.limit == 2
    assert limiter.stats.successes == 0


def test_release_is_idempotent() -> None:
    limiter = AdaptiveLimiter(initial=2)
    permit = limiter.acquire()
    permit.release()
    permit.release()
    assert limiter.in_flight == 0
    assert limiter.stats.successes == 1


def test_sheds_beyond_the_limit() -> None:
    limiter = AdaptiveLimiter(initial=2, min_limit=1, name="db")
    a, b = limiter.acquire(), limiter.acquire()
    assert limiter.in_flight == 2
    with pytest.raises(LoadShed) as info:
        limiter.acquire()
    shed = info.value
    assert isinstance(shed, BulkheadFull)
    assert (shed.limit, shed.in_flight, shed.priority, shed.name) == (2, 2, "normal", "db")
    assert limiter.stats.shed == 1
    a.release()
    limiter.acquire().release()  # a slot is free again
    b.release()


def test_latency_target_with_fake_clock(fake_clock: FakeClock) -> None:
    limiter = AdaptiveLimiter(initial=10, target_latency=0.5, backoff_ratio=0.5)
    permit = limiter.acquire()
    fake_clock.advance(0.2)
    permit.release()
    assert limiter.stats.successes == 1 and limiter.limit == 10
    slow = limiter.acquire()
    fake_clock.advance(0.9)
    slow.release()
    assert limiter.limit == 5
    assert limiter.stats.drops == 1


def test_no_target_latency_ignores_slowness(fake_clock: FakeClock) -> None:
    limiter = AdaptiveLimiter(initial=10, backoff_ratio=0.5)
    permit = limiter.acquire()
    fake_clock.advance(1000)
    permit.release()
    assert limiter.limit == 10


def test_priority_shedding() -> None:
    limiter = AdaptiveLimiter(initial=10, low_priority_share=0.5)
    permits = [limiter.acquire() for _ in range(5)]
    with pytest.raises(LoadShed) as info:
        limiter.acquire(priority="low")
    assert info.value.priority == "low"
    permits.append(limiter.acquire(priority="normal"))  # normal still fits
    permits.extend(limiter.acquire(priority="critical") for _ in range(4))
    assert limiter.in_flight == 10
    with pytest.raises(LoadShed):
        limiter.acquire(priority="critical")  # even critical stops at the full limit


def test_low_priority_always_gets_one_slot() -> None:
    limiter = AdaptiveLimiter(initial=1, low_priority_share=0.1)
    limiter.acquire(priority="low").release()


def test_events() -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    limiter = AdaptiveLimiter(initial=2, window=1, backoff_ratio=0.5, name="svc")
    with events.listening(lambda e: seen.append((e.name, e.fields))):
        limiter.acquire().release()  # 2 -> 3
        limiter.acquire().release(error=TimeoutError())  # 3 -> 1
        held = limiter.acquire()
        with pytest.raises(LoadShed):
            limiter.acquire()
        held.release(ignore=True)
    changes = [f for n, f in seen if n == "adaptive.limit_changed"]
    assert changes == [{"old": 2, "new": 3}, {"old": 3, "new": 1}]
    shed = [f for n, f in seen if n == "adaptive.shed"]
    assert shed == [{"limit": 1, "in_flight": 1}]


def test_sync_decorator_and_guard() -> None:
    limiter = AdaptiveLimiter(initial=1, max_limit=1)

    @limiter
    def work(x: int) -> int:
        assert limiter.in_flight == 1
        return x * 2

    assert work(2) == 4
    assert work.__name__ == "work"
    assert limiter.in_flight == 0
    permit = limiter.acquire()
    with pytest.raises(LoadShed):
        work(1)
    permit.release()


async def test_async_decorator_and_context_manager() -> None:
    limiter = AdaptiveLimiter(initial=2, backoff_ratio=0.5)

    @limiter
    async def work(x: int) -> int:
        await asyncio.sleep(0.01)
        return x

    results = await asyncio.gather(*(work(i) for i in range(3)), return_exceptions=True)
    assert results[:2] == [0, 1]
    assert isinstance(results[2], LoadShed)

    with pytest.raises(TimeoutError):
        async with limiter.acquire():
            raise TimeoutError
    assert limiter.limit == 1
    assert limiter.in_flight == 0


async def test_async_guard_priority() -> None:
    limiter = AdaptiveLimiter(initial=2, low_priority_share=0.5)

    @limiter.guard(priority="low")
    async def background() -> str:
        return "ok"

    held = limiter.acquire()
    with pytest.raises(LoadShed):
        await background()
    held.release()
    assert await background() == "ok"


def test_thread_safety_smoke() -> None:
    limiter = AdaptiveLimiter(initial=8, max_limit=16)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for n in range(300):
                try:
                    permit = limiter.acquire()
                except LoadShed:
                    continue
                permit.release(error=TimeoutError() if (n + i) % 50 == 0 else None)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    stats = limiter.stats
    assert stats.in_flight == 0
    assert 1 <= stats.limit <= 16
    assert stats.admitted == stats.successes + stats.drops


def test_fork_reset() -> None:
    limiter = AdaptiveLimiter(initial=5, backoff_ratio=0.5)
    limiter.acquire()
    limiter.acquire().release(error=TimeoutError())
    limiter._reset_after_fork()
    assert limiter.limit == 5 and limiter.in_flight == 0


# ------------------------------------------------------------------------- per key


def test_pool_creates_one_limiter_per_key() -> None:
    made: list[str] = []

    def factory(key: object) -> AdaptiveLimiter:
        made.append(str(key))
        return AdaptiveLimiter(initial=1)

    pool = PerKeyLimiter(factory)
    assert pool.get("a") is pool.get("a")
    assert pool["b"] is not pool["a"]
    assert made == ["a", "b"]
    assert "a" in pool and len(pool) == 2


def test_pool_isolates_keys() -> None:
    pool = PerKeyLimiter(lambda key: AdaptiveLimiter(initial=1))
    held = pool.get("noisy").acquire()
    with pytest.raises(LoadShed):
        pool.get("noisy").acquire()
    pool.get("quiet").acquire().release()
    held.release()


def test_pool_lru_eviction() -> None:
    pool = PerKeyLimiter(lambda key: AdaptiveLimiter(initial=1), max_keys=2)
    pool.get("a")
    pool.get("b")
    pool.get("a")  # b is now the least recently used
    pool.get("c")
    assert pool.keys() == ["a", "c"]


def test_pool_never_evicts_busy_keys() -> None:
    pool = PerKeyLimiter(lambda key: AdaptiveLimiter(initial=2), max_keys=2)
    permit = pool.get("a").acquire()
    pool.get("b")
    pool.get("c")
    assert "a" in pool  # busy: kept even though it is the oldest
    assert "b" not in pool
    permit.release()
    pool.get("d")
    assert "a" not in pool
    assert len(pool) == 2


def test_pool_discard_clear_and_validation() -> None:
    pool = PerKeyLimiter(lambda key: AdaptiveLimiter())
    pool.get("a")
    assert pool.discard("a") and not pool.discard("a")
    pool.get("b")
    pool.clear()
    assert len(pool) == 0
    with pytest.raises(ValueError):
        PerKeyLimiter(lambda key: 1, max_keys=0)


def test_per_key_decorator_with_bulkhead() -> None:
    gate = threading.Event()
    started = threading.Event()

    @per_key(lambda tenant: Bulkhead(1, max_waiting=0), key=lambda tenant: tenant)
    def handle(tenant: str) -> str:
        started.set()
        gate.wait(5)
        return tenant

    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(handle("acme")))
    t.start()
    assert started.wait(5)
    with pytest.raises(BulkheadFull):
        handle("acme")
    gate.set()
    assert handle("globex") == "globex"  # another tenant is unaffected
    t.join()
    assert result == ["acme"]


async def test_per_key_decorator_async_with_adaptive() -> None:
    @per_key(lambda k: AdaptiveLimiter(initial=1), key=lambda k, **_: k)
    async def fetch(k: str) -> str:
        await asyncio.sleep(0.01)
        return k

    results = await asyncio.gather(fetch("a"), fetch("a"), fetch("b"), return_exceptions=True)
    assert results[0] == "a" and results[2] == "b"
    assert isinstance(results[1], LoadShed)
    assert inspect.iscoroutinefunction(fetch)


def test_per_key_with_rate_limiter() -> None:
    @per_key(lambda user: RateLimiter(1, 60), key=lambda user: user, max_keys=10)
    def call(user: str) -> str:
        return user

    assert call("u1") == "u1"
    with pytest.raises(RateLimitExceeded):
        call("u1")
    assert call("u2") == "u2"


def test_rate_limit_key_option_is_independent() -> None:
    from python_wrap.concurrency import rate_limit

    @rate_limit(1, 60, key=lambda user: user)
    def call(user: str) -> str:
        return user

    assert call("a") == "a" and call("b") == "b"
    with pytest.raises(RateLimitExceeded):
        call("a")
