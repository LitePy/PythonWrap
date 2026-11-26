"""Async-native shared backends: breaker state, leases, bulkheads, idempotency, amemoize."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from python_wrap.async_cache import AsyncMemoryStore, amemoize
from python_wrap.circuit import CircuitBreaker, CircuitState
from python_wrap.coordination import AlreadyRunning, AsyncRedisLock, AsyncRedisSemaphore, Lease
from python_wrap.errors import CircuitOpenError
from python_wrap.idempotency import idempotent
from python_wrap.shared import AsyncRedisBreakerState, SharedBackends, SharedBulkhead
from python_wrap.testing import fake_clock


class AsyncEval:
    """A redis.asyncio stand-in whose ``eval`` returns a scripted answer."""

    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, 0

    async def eval(self, *args: Any) -> Any:
        await asyncio.sleep(0)
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(args) if callable(self.answer) else self.answer


def breaker_row(state: str = "closed", admitted: int = 1, old: str | None = None) -> list[Any]:
    return [admitted, state, 1, 0, 2000, 0, 0, 0, 0, 0, old or state]


@pytest.fixture
def no_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("asyncio.to_thread must not be used with async backends")

    monkeypatch.setattr(asyncio, "to_thread", forbidden)


def test_breaker_and_bulkhead_on_async_backends_never_use_threads(no_threads: None) -> None:
    breaker_client, lease_client = AsyncEval(breaker_row()), AsyncEval(1)
    breaker = CircuitBreaker(name="vendor", backend=AsyncRedisBreakerState(breaker_client))
    bulkhead = SharedBulkhead(4, backend=AsyncRedisSemaphore(lease_client), name="vendor")

    @breaker
    async def call() -> int:
        await bulkhead.acquire_async()
        try:
            return 1
        finally:
            await bulkhead.release_async()

    async def main() -> int:
        return sum([await call() for _ in range(1000)])

    assert asyncio.run(main()) == 1000
    assert breaker_client.calls < 10  # closed state cached, successes batched (gap 4)
    assert lease_client.calls == 2000  # acquire + release
    assert breaker.state is CircuitState.CLOSED  # last answer seen from the store


def test_async_breaker_backend_rejects_open_and_reports_transition() -> None:
    client = AsyncEval(breaker_row("open", admitted=0, old="closed"))
    changes: list[tuple[str, str]] = []
    breaker = CircuitBreaker(
        name="open-one",
        backend=AsyncRedisBreakerState(client),
        on_state_change=lambda b, old, new: changes.append((old.value, new.value)),
    )

    @breaker
    async def call() -> None: ...

    with pytest.raises(CircuitOpenError) as info:
        asyncio.run(call())
    assert info.value.retry_after == 2.0
    assert changes == [("closed", "open")]
    assert breaker.state is CircuitState.OPEN


def test_async_breaker_backend_refuses_sync_use() -> None:
    breaker = CircuitBreaker(name="sync", backend=AsyncRedisBreakerState(AsyncEval(breaker_row())))

    with pytest.raises(TypeError, match="async"):
        breaker(lambda: 1)()
    with pytest.raises(TypeError, match="async"):
        breaker.reset()
    asyncio.run(breaker.areset())
    asyncio.run(breaker.aforce_open())


def test_async_breaker_backend_error_falls_back_to_local_state() -> None:
    client = AsyncEval(ConnectionError("store down"))
    breaker = CircuitBreaker(
        name="down",
        failure_threshold=1,
        backend=AsyncRedisBreakerState(client),
        on_backend_error="local",
    )

    @breaker
    async def boom() -> None:
        raise ValueError

    with pytest.raises(ValueError):
        asyncio.run(boom())
    with pytest.raises(CircuitOpenError):
        asyncio.run(boom())


def test_async_lease_backend_is_awaited_and_refuses_sync_with(no_threads: None) -> None:
    held: dict[str, str] = {}

    def lua(args: tuple[Any, ...]) -> int:
        _, _, key, op, token, *_ = args
        if op == "release":
            return int(held.pop(key, None) == token)
        if op == "renew":
            return int(held.get(key) == token)
        if key in held and held[key] != token:
            return 0
        held[key] = token
        return 1

    backend = AsyncRedisLock(AsyncEval(lua))

    async def main() -> None:
        async with Lease("job", backend=backend, ttl=1) as lease:
            await lease.check_async()
            with pytest.raises(AlreadyRunning):
                async with Lease("job", backend=backend, ttl=1):
                    pass
        assert held == {}

    asyncio.run(main())
    with pytest.raises(TypeError, match="async with"), Lease("job", backend=backend):
        pass


def test_shared_bulkhead_with_async_backend_refuses_sync_acquire() -> None:
    bulkhead = SharedBulkhead(1, backend=AsyncRedisSemaphore(AsyncEval(1)), name="b")
    with pytest.raises(TypeError, match="async"):
        bulkhead.acquire()


def test_shared_backends_pick_async_classes_for_an_async_client() -> None:
    from python_wrap.quotas import AsyncRedisWindow

    shared = SharedBackends(client=AsyncEval(1))
    assert isinstance(shared.breaker(), AsyncRedisBreakerState)
    assert isinstance(shared.lease_backend(), AsyncRedisSemaphore)
    assert isinstance(shared.rate_window("x", 1, 1), AsyncRedisWindow)
    assert shared.budget_backend("x") is None


def test_idempotent_awaits_an_async_result_store(no_threads: None) -> None:
    store, calls = AsyncMemoryStore(), []

    @idempotent(key=lambda n: f"k{n}", store=store)
    async def work(n: int) -> int:
        calls.append(n)
        return n * 2

    async def main() -> None:
        assert await work(2) == await work(2) == 4

    asyncio.run(main())
    assert calls == [2]
    with pytest.raises(TypeError, match="async def"):
        idempotent(store=AsyncMemoryStore())(lambda: 1)


def test_amemoize_serves_stale_while_one_refresh_runs() -> None:
    calls: list[int] = []

    @amemoize(ttl=10, stale_ttl=50)
    async def value() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        return len(calls)

    async def main(clock: Any) -> None:
        assert await value() == 1
        clock.advance(20)  # stale, inside stale_ttl
        assert [await value(), await value()] == [1, 1]
        for _ in range(5):
            await asyncio.sleep(0)
        assert await value() == 2  # the single refresh landed
        clock.advance(100)  # past ttl + stale_ttl: a plain miss
        assert await value() == 3

    with fake_clock() as clock:
        asyncio.run(main(clock))
    assert len(calls) == 3


def test_amemoize_failed_refresh_keeps_stale_value(caplog: pytest.LogCaptureFixture) -> None:
    fail = False

    @amemoize(ttl=10, stale_ttl=50)
    async def value() -> str:
        if fail:
            raise RuntimeError("upstream")
        return "v1"

    async def main(clock: Any) -> None:
        nonlocal fail
        assert await value() == "v1"
        fail = True
        clock.advance(20)
        assert await value() == "v1"
        for _ in range(5):
            await asyncio.sleep(0)
        assert await value() == "v1"

    with fake_clock() as clock:
        asyncio.run(main(clock))
    assert "background refresh" in caplog.text


def test_amemoize_caches_listed_exceptions_for_exception_ttl() -> None:
    calls: list[int] = []

    @amemoize(ttl=60, cache_exceptions=LookupError, exception_ttl=5)
    async def find(n: int) -> int:
        calls.append(n)
        raise KeyError(n)

    async def main(clock: Any) -> None:
        for _ in range(3):
            with pytest.raises(KeyError):
                await find(1)
        assert calls == [1]
        clock.advance(6)
        with pytest.raises(KeyError):
            await find(1)
        assert calls == [1, 1]

    with fake_clock() as clock:
        asyncio.run(main(clock))


def test_amemoize_does_not_cache_unlisted_exceptions_and_validates() -> None:
    calls: list[int] = []

    @amemoize(ttl=60, cache_exceptions=LookupError)
    async def boom() -> None:
        calls.append(1)
        raise ValueError

    async def main() -> None:
        for _ in range(2):
            with pytest.raises(ValueError):
                await boom()

    asyncio.run(main())
    assert calls == [1, 1]
    with pytest.raises(ValueError, match="stale_ttl requires ttl"):
        amemoize(stale_ttl=5)
    with pytest.raises(TypeError):
        amemoize(cache_exceptions=int)  # type: ignore[arg-type]
