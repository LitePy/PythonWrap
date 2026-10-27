from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading

import pytest

from conftest import FakeClock
from python_wrap.circuit import CircuitBreaker, CircuitState, circuit_breaker
from python_wrap.errors import CircuitOpenError


def boom() -> None:
    raise ValueError("boom")


def ok() -> str:
    return "ok"


def trip(breaker: CircuitBreaker, n: int) -> None:
    for _ in range(n):
        with pytest.raises(ValueError):
            breaker.call(boom)


def test_initial_state_and_str_enum() -> None:
    b = CircuitBreaker()
    assert b.state is CircuitState.CLOSED
    assert b.state == "closed"
    assert CircuitState.HALF_OPEN.value == "half_open"
    assert b.failure_count == 0
    assert "closed" in repr(b)


@pytest.mark.parametrize(
    "kwargs",
    [{"failure_threshold": 0}, {"recovery_timeout": -1}, {"half_open_max_calls": 0}],
)
def test_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(**kwargs)  # type: ignore[arg-type]


def test_opens_after_threshold_and_rejects(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
    trip(b, 2)
    assert b.state is CircuitState.CLOSED
    assert b.failure_count == 2
    trip(b, 1)
    assert b.state is CircuitState.OPEN

    calls = []
    fake_clock.advance(4)
    with pytest.raises(CircuitOpenError) as info:
        b.call(lambda: calls.append(1))
    assert calls == []
    assert info.value.retry_after == pytest.approx(6)


def test_success_resets_consecutive_count() -> None:
    b = CircuitBreaker(failure_threshold=3)
    trip(b, 2)
    assert b.call(ok) == "ok"
    assert b.failure_count == 0
    trip(b, 2)
    assert b.state is CircuitState.CLOSED


def test_non_matching_exceptions_not_counted() -> None:
    b = CircuitBreaker(failure_threshold=1, on=ValueError)
    with pytest.raises(KeyError):
        b.call(lambda: (_ for _ in ()).throw(KeyError()))
    assert b.failure_count == 0
    assert b.state is CircuitState.CLOSED


def test_half_open_success_closes(fake_clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    b = CircuitBreaker(failure_threshold=2, recovery_timeout=10, name="svc")
    with caplog.at_level(logging.INFO, logger="python_wrap"):
        trip(b, 2)
        fake_clock.advance(10)
        assert b.state is CircuitState.HALF_OPEN
        assert b.call(ok) == "ok"
    assert b.state is CircuitState.CLOSED
    assert b.failure_count == 0
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.INFO, logging.INFO]
    assert "svc" in caplog.records[0].getMessage()


def test_half_open_failure_reopens(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
    trip(b, 2)
    fake_clock.advance(10)
    trip(b, 1)
    assert b.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError) as info:
        b.call(ok)
    assert info.value.retry_after == pytest.approx(10)


def test_half_open_limits_concurrent_trials(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=5, half_open_max_calls=2)
    trip(b, 1)
    fake_clock.advance(5)

    def nested() -> str:
        # Two trials are in flight (this one and the outer), so a third is rejected.
        with pytest.raises(CircuitOpenError):
            b.call(ok)
        return "inner"

    def outer() -> str:
        return b.call(nested)

    assert b.call(outer) == "inner"
    assert b.state is CircuitState.CLOSED


def test_half_open_non_counted_exception_frees_slot(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=5, on=ValueError)
    trip(b, 1)
    fake_clock.advance(5)

    def raise_key() -> None:
        raise KeyError

    with pytest.raises(KeyError):
        b.call(raise_key)
    assert b.state is CircuitState.HALF_OPEN
    assert b.call(ok) == "ok"
    assert b.state is CircuitState.CLOSED


def test_reset(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=1)
    trip(b, 1)
    b.reset()
    assert b.state is CircuitState.CLOSED
    assert b.failure_count == 0
    assert b.call(ok) == "ok"
    b.reset()  # already closed: no-op


def test_stale_outcome_after_reset_ignored() -> None:
    b = CircuitBreaker(failure_threshold=1)

    def slow_fail() -> None:
        b.reset()
        raise ValueError

    with pytest.raises(ValueError):
        b.call(slow_fail)
    assert b.state is CircuitState.CLOSED


def test_shared_breaker_decorates_several_functions() -> None:
    b = CircuitBreaker(failure_threshold=2, recovery_timeout=100)

    @b
    def a() -> None:
        raise ValueError

    @b
    def c() -> str:
        return "c"

    for _ in range(2):
        with pytest.raises(ValueError):
            a()
    with pytest.raises(CircuitOpenError):
        c()
    assert a.__name__ == "a"


def test_circuit_breaker_decorator_forms(fake_clock: FakeClock) -> None:
    @circuit_breaker(failure_threshold=1, recovery_timeout=5)
    def f(fail: bool) -> str:
        if fail:
            raise ValueError
        return "ok"

    @circuit_breaker
    def g() -> str:
        return "g"

    assert g() == "g"
    assert g.breaker.state is CircuitState.CLOSED  # type: ignore[attr-defined]
    assert g.breaker is not f.breaker  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        f(True)
    with pytest.raises(CircuitOpenError):
        f(False)
    assert f.breaker.name.endswith("f")  # type: ignore[attr-defined]
    fake_clock.advance(5)
    assert f(False) == "ok"
    assert f.breaker.state is CircuitState.CLOSED  # type: ignore[attr-defined]


def test_methods() -> None:
    class Client:
        @circuit_breaker(failure_threshold=1)
        def get(self) -> None:
            raise ValueError

    c = Client()
    with pytest.raises(ValueError):
        c.get()
    with pytest.raises(CircuitOpenError):
        c.get()


def test_async_flow(fake_clock: FakeClock) -> None:
    @circuit_breaker(failure_threshold=2, recovery_timeout=10)
    async def fetch(fail: bool) -> str:
        await asyncio.sleep(0)
        if fail:
            raise ValueError
        return "ok"

    assert inspect.iscoroutinefunction(fetch)

    async def scenario() -> None:
        for _ in range(2):
            with pytest.raises(ValueError):
                await fetch(True)
        with pytest.raises(CircuitOpenError):
            await fetch(False)
        fake_clock.advance(10)
        assert await fetch(False) == "ok"

    asyncio.run(scenario())
    assert fetch.breaker.state is CircuitState.CLOSED  # type: ignore[attr-defined]


def test_async_call_and_non_counted(fake_clock: FakeClock) -> None:
    b = CircuitBreaker(failure_threshold=1, on=ValueError, recovery_timeout=1)

    async def work(x: int) -> int:
        if x == 1:
            raise KeyError
        if x == 2:
            raise ValueError
        return x

    async def scenario() -> None:
        assert await b.call(work, 0) == 0
        with pytest.raises(KeyError):
            await b.call(work, 1)
        assert b.state is CircuitState.CLOSED
        with pytest.raises(ValueError):
            await b.call(work, 2)
        assert b.state is CircuitState.OPEN
        fake_clock.advance(1)
        with pytest.raises(KeyError):
            await b.call(work, 1)
        assert b.state is CircuitState.HALF_OPEN

    asyncio.run(scenario())


def test_thread_safety_exact_counts() -> None:
    b = CircuitBreaker(failure_threshold=10_000)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(100):
            with contextlib.suppress(ValueError):
                b.call(boom)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.failure_count == 800
    assert b.state is CircuitState.CLOSED
