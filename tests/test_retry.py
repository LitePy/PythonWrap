from __future__ import annotations

import asyncio
import inspect
import logging
import random
import warnings
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap.retry import RetryState, fallback, retry, retry_on_exception, suppress


class Flaky:
    """Fails ``failures`` times, then returns ``"ok"``."""

    def __init__(self, failures: int, exc: type[BaseException] = ValueError) -> None:
        self.failures = failures
        self.exc = exc
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc(f"fail {self.calls}")
        return "ok"


def test_bare_and_parameterised_and_call_forms(fake_clock: FakeClock) -> None:
    f1 = Flaky(1)
    assert retry(f1)() == "ok"
    f2 = Flaky(1)
    assert retry(max_attempts=3, delay=1.0)(f2)() == "ok"
    f3 = Flaky(1)
    assert retry(f3, max_attempts=3, delay=0.5)() == "ok"
    assert fake_clock.sleeps == [1.0, 1.0, 0.5]

    @retry
    def bare() -> int:
        return 1

    assert bare() == 1
    assert bare.__name__ == "bare"


def test_exhaustion_reraises_last_exception_unchanged(fake_clock: FakeClock) -> None:
    f = Flaky(10)
    with pytest.raises(ValueError, match="fail 3") as info:
        retry(f, max_attempts=3)()
    assert f.calls == 3
    assert fake_clock.sleeps == [1.0, 1.0]
    assert info.traceback  # original traceback kept


def test_backoff_and_max_delay(fake_clock: FakeClock) -> None:
    f = Flaky(10)
    with pytest.raises(ValueError):
        retry(f, max_attempts=6, delay=1, backoff=2, max_delay=5)()
    assert fake_clock.sleeps == [1, 2, 4, 5, 5]


def test_jitter_added(fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float, float]] = []

    def fake_uniform(a: float, b: float) -> float:
        seen.append((a, b))
        return 0.25

    monkeypatch.setattr(random, "uniform", fake_uniform)
    f = Flaky(2)
    retry(f, max_attempts=3, delay=1, backoff=2, jitter=0.5)()
    assert fake_clock.sleeps == [1.25, 2.25]
    assert seen == [(0, 0.5), (0, 0.5)]


def test_unlimited_attempts(fake_clock: FakeClock) -> None:
    f = Flaky(20)
    assert retry(f, max_attempts=None, delay=0)() == "ok"
    assert f.calls == 21


def test_huge_backoff_does_not_overflow(fake_clock: FakeClock) -> None:
    f = Flaky(1100)
    assert retry(f, max_attempts=None, delay=1, backoff=2, max_delay=10)() == "ok"
    assert max(fake_clock.sleeps) == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"delay": -1},
        {"backoff": 0.5},
        {"jitter": -0.1},
        {"max_delay": -1},
    ],
)
def test_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        retry(**kwargs)
    with pytest.raises(ValueError):
        retry_on_exception(**kwargs)


def test_on_filters_exceptions(fake_clock: FakeClock) -> None:
    f = Flaky(2, KeyError)
    with pytest.raises(KeyError):
        retry(f, on=ValueError)()
    assert f.calls == 1
    assert fake_clock.sleeps == []

    g = Flaky(1, KeyError)
    assert retry(g, on=(ValueError, KeyError))() == "ok"


def test_retry_if_predicate(fake_clock: FakeClock) -> None:
    f = Flaky(5)
    with pytest.raises(ValueError):
        retry(f, retry_if=lambda e: "fail 2" not in str(e))()
    assert f.calls == 2


def test_baseexception_not_retried_by_default(fake_clock: FakeClock) -> None:
    f = Flaky(1, KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        retry(f)()
    assert f.calls == 1
    g = Flaky(1, KeyboardInterrupt)
    assert retry(g, on=KeyboardInterrupt)() == "ok"


def test_on_retry_state_and_logging(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    states: list[RetryState] = []
    f = Flaky(2)
    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        retry(f, delay=2, on_retry=states.append)()
    assert [s.attempt for s in states] == [1, 2]
    assert states[0].next_delay == 2
    assert isinstance(states[0].exception, ValueError)
    assert states[0].elapsed == 0
    assert states[1].elapsed == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    with pytest.raises(AttributeError):
        states[0].attempt = 5  # type: ignore[misc]


def test_custom_logger_name(fake_clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="custom"):
        retry(Flaky(1), logger="custom", max_attempts=None)()
    assert caplog.records[0].name == "custom"


def test_method_forms(fake_clock: FakeClock) -> None:
    class C:
        n = 0

        @retry(delay=0)
        def inst(self) -> int:
            C.n += 1
            if C.n < 2:
                raise ValueError
            return C.n

        @classmethod
        @retry(delay=0)
        def cm(cls) -> str:
            return cls.__name__

        @staticmethod
        @retry(delay=0)
        def sm() -> int:
            return 7

    assert C().inst() == 2
    assert C.cm() == "C"
    assert C.sm() == 7


def test_async_retry(fake_clock: FakeClock) -> None:
    calls = 0

    @retry(delay=1, backoff=2, max_attempts=4)
    async def go() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ValueError
        return "ok"

    assert inspect.iscoroutinefunction(go)
    assert asyncio.run(go()) == "ok"
    assert fake_clock.sleeps == [1, 2]


def test_async_exhaustion_and_filter(fake_clock: FakeClock) -> None:
    @retry(max_attempts=2, on=ValueError)
    async def bad() -> None:
        raise ValueError("x")

    @retry(on=ValueError)
    async def other() -> None:
        raise KeyError("y")

    with pytest.raises(ValueError):
        asyncio.run(bad())
    with pytest.raises(KeyError):
        asyncio.run(other())
    assert fake_clock.sleeps == [1.0]


# -- retry_on_exception ------------------------------------------------------------


def test_retry_on_exception_deprecated_alias(fake_clock: FakeClock) -> None:
    f = Flaky(2)
    with pytest.warns(DeprecationWarning, match=r"retry\(on=") as rec:
        wrapped = retry_on_exception(ValueError)(f)
    assert rec[0].filename == __file__
    assert wrapped() == "ok"
    assert fake_clock.sleeps == [0.0, 0.0]  # delay defaults to 0


def test_retry_on_exception_defaults_and_limits() -> None:
    f = Flaky(10)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wrapped = retry_on_exception()(f)
    with pytest.raises(ValueError):
        wrapped()
    assert f.calls == 3


# -- fallback ----------------------------------------------------------------------


def test_fallback_value_and_handler(caplog: pytest.LogCaptureFixture) -> None:
    @fallback(0)
    def boom() -> int:
        raise ValueError("x")

    @fallback(handler=lambda e: str(e))
    def boom2() -> str:
        raise ValueError("msg")

    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        assert boom() == 0
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].levelno == logging.WARNING
    assert boom2() == "msg"


def test_fallback_none_value_and_passthrough() -> None:
    @fallback(None, on=KeyError)
    def f(x: int) -> int | None:
        if x == 0:
            raise KeyError
        if x == 1:
            raise ValueError
        return x

    assert f(0) is None
    assert f(5) == 5
    with pytest.raises(ValueError):
        f(1)


def test_fallback_requires_exactly_one() -> None:
    with pytest.raises(ValueError):
        fallback()  # type: ignore[call-overload]
    with pytest.raises(ValueError):
        fallback(1, handler=lambda e: 2)  # type: ignore[call-overload]


def test_fallback_async() -> None:
    @fallback(handler=lambda e: -1)
    async def boom() -> int:
        raise RuntimeError

    @fallback("d")
    async def fine() -> str:
        return "v"

    assert inspect.iscoroutinefunction(boom)
    assert asyncio.run(boom()) == -1
    assert asyncio.run(fine()) == "v"


# -- suppress ----------------------------------------------------------------------


def test_suppress_forms(caplog: pytest.LogCaptureFixture) -> None:
    @suppress
    def a() -> int:
        raise ValueError

    @suppress(KeyError)
    def b(x: int) -> int:
        if x:
            raise KeyError
        raise ValueError

    @suppress(KeyError, ValueError)
    def c() -> int:
        raise ValueError

    with caplog.at_level(logging.DEBUG, logger="python_wrap"):
        assert a() is None
    assert caplog.records[0].levelno == logging.DEBUG
    assert b(1) is None
    with pytest.raises(ValueError):
        b(0)
    assert c() is None
    assert a.__name__ == "a"


def test_suppress_async() -> None:
    @suppress
    async def a() -> int:
        raise ValueError

    @suppress(KeyError)
    async def b() -> int:
        return 3

    assert inspect.iscoroutinefunction(a)
    assert asyncio.run(a()) is None
    assert asyncio.run(b()) == 3
