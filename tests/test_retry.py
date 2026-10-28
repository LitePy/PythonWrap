from __future__ import annotations

import asyncio
import contextlib
import email.utils
import functools
import inspect
import logging
import random
import threading
import time
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.config import configure, configured
from python_wrap.retry import (
    RetryBudget,
    RetryBudgetStats,
    RetryState,
    RetryStats,
    fallback,
    retry,
    retry_after_header,
    retry_on_exception,
    retry_stats,
    retrying,
    suppress,
)
from python_wrap.testing import disable_retries, fast_mode


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
    with pytest.raises(KeyboardInterrupt):  # never retried, even when listed
        retry(g, on=KeyboardInterrupt)()
    assert g.calls == 1


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


# ------------------------------------------------------------------ delay_from / max_time


class FakeResponse:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class RateLimited(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("429")
        self.response = FakeResponse(headers)


def test_delay_from_overrides_backoff_and_falls_back(fake_clock: FakeClock) -> None:
    hints = iter([7.0, None, 2.0])
    f = Flaky(3)
    retry(f, max_attempts=5, delay=1, backoff=2, delay_from=lambda e: next(hints))()
    assert fake_clock.sleeps == [7.0, 2.0, 2.0]  # attempt 2 used backoff (1 * 2)


def test_delay_from_capped_by_max_delay_and_no_jitter(fake_clock: FakeClock) -> None:
    retry(Flaky(1), delay_from=lambda e: 60, max_delay=5, jitter=3)()
    assert fake_clock.sleeps == [5]


def test_delay_from_bad_values_use_backoff(fake_clock: FakeClock) -> None:
    def boom(exc: BaseException) -> float | None:
        raise RuntimeError("nope")

    retry(Flaky(1), delay=2, delay_from=boom)()
    retry(Flaky(1), delay=3, delay_from=lambda e: float("nan"))()
    retry(Flaky(1), delay=4, delay_from=lambda e: "x")()  # type: ignore[arg-type,return-value]
    retry(Flaky(1), delay_from=lambda e: -5)()
    assert fake_clock.sleeps == [2, 3, 4, 0.0]


def test_retry_after_recipe(fake_clock: FakeClock) -> None:
    calls = []

    @retry(on=RateLimited, delay_from=retry_after_header, max_attempts=3)
    def get() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited({"retry-after": "4"})
        if len(calls) == 2:
            raise RateLimited({})
        return "ok"

    assert get() == "ok"
    assert fake_clock.sleeps == [4.0, 1.0]


def test_retry_after_header_parsing() -> None:
    assert retry_after_header(RateLimited({"Retry-After": "12"})) == 12.0
    assert retry_after_header(RateLimited({"Retry-After": "1.5"})) == 1.5
    date = email.utils.formatdate(time.time() + 30, usegmt=True)
    parsed = retry_after_header(RateLimited({"Retry-After": date}))
    assert parsed is not None and 25 < parsed <= 30
    past = email.utils.formatdate(time.time() - 100, usegmt=True)
    assert retry_after_header(RateLimited({"Retry-After": past})) == 0.0
    assert retry_after_header(RateLimited({"Retry-After": "-3"})) == 0.0
    assert retry_after_header(RateLimited({"Retry-After": "garbage"})) is None
    assert retry_after_header(RateLimited({"Retry-After": "nan"})) is None
    assert retry_after_header(RateLimited({})) is None
    assert retry_after_header(ValueError("no response")) is None

    class WithAttr(Exception):
        retry_after = 9

    assert retry_after_header(WithAttr()) == 9.0

    class BadAttr(Exception):
        retry_after = True

    assert retry_after_header(BadAttr()) is None

    class Exploding(Exception):
        @property
        def response(self) -> Any:
            raise RuntimeError("boom")

    assert retry_after_header(Exploding()) is None


def test_max_time_stops_before_overrunning(fake_clock: FakeClock) -> None:
    f = Flaky(100)
    with pytest.raises(ValueError, match="fail 4"):
        retry(f, max_attempts=None, delay=1, backoff=2, max_time=10)()
    # waits 1, 2, 4 = 7s elapsed; next wait 8 would end at 15 > 10
    assert fake_clock.sleeps == [1, 2, 4]
    assert f.calls == 4


def test_max_time_counts_call_duration(fake_clock: FakeClock) -> None:
    calls = []

    @retry(max_attempts=None, delay=1, max_time=5)
    def slow() -> None:
        calls.append(1)
        fake_clock.advance(2)
        raise ValueError

    with pytest.raises(ValueError):
        slow()
    # t=2 fail, wait to 3, t=5 fail: 5+1 > 5, stop
    assert len(calls) == 2
    stats = retry_stats(slow)
    assert stats is not None and stats.gave_up == 1


def test_max_time_with_delay_from(fake_clock: FakeClock) -> None:
    f = Flaky(5)
    with pytest.raises(ValueError):
        retry(f, max_attempts=5, delay_from=lambda e: 100, max_time=30)()
    assert f.calls == 1
    assert fake_clock.sleeps == []


def test_max_time_validation() -> None:
    with pytest.raises(ValueError, match="max_time"):
        retry(max_time=-1)
    with pytest.raises(ValueError, match="max_time"):
        retrying(max_time=-1)
    retry(max_time=0)(Flaky(0))()


# ------------------------------------------------------------------ cancellation


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_never_retries_exit_exceptions_even_with_baseexception(
    fake_clock: FakeClock, exc: type[BaseException]
) -> None:
    f = Flaky(1, exc)
    with pytest.raises(exc):
        retry(f, on=BaseException)()
    assert f.calls == 1
    assert fake_clock.sleeps == []


def test_baseexception_still_retries_other_baseexceptions(fake_clock: FakeClock) -> None:
    class Odd(BaseException):
        pass

    assert retry(Flaky(1, Odd), on=BaseException)() == "ok"


def test_async_cancelled_error_is_not_retried(fake_clock: FakeClock) -> None:
    calls = []

    @retry(on=BaseException, max_attempts=5)
    async def f() -> None:
        calls.append(1)
        raise asyncio.CancelledError

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await f()

    asyncio.run(main())
    assert calls == [1]
    assert fake_clock.sleeps == []


def test_cancel_during_backoff_sleep_is_immediate() -> None:
    calls = []

    @retry(max_attempts=5, delay=3600)
    async def f() -> None:
        calls.append(1)
        raise ValueError

    async def main() -> float:
        task = asyncio.ensure_future(f())
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == [1]  # now sleeping in the backoff
        started = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.perf_counter() - started

    assert asyncio.run(main()) < 1
    assert calls == [1]
    stats = retry_stats(f)
    assert stats is not None and stats.total_sleep == 0  # cancelled sleep not counted


def test_retrying_does_not_retry_cancellation(fake_clock: FakeClock) -> None:
    runs = 0
    with pytest.raises(KeyboardInterrupt):
        for attempt in retrying(on=BaseException):
            runs += 1
            with attempt:
                raise KeyboardInterrupt
    assert runs == 1


# ------------------------------------------------------------------ logging


def test_log_level_and_exc_info(fake_clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="python_wrap"):
        retry(Flaky(1), log_level=logging.INFO, log_exc_info=True)()
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert record.exc_info is not None and record.exc_info[0] is ValueError


def test_log_level_none_silences(fake_clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="python_wrap"):
        retry(Flaky(2), log_level=None)()
    assert caplog.records == []


def test_default_log_has_no_exc_info(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        retry(Flaky(1))()
    assert caplog.records[0].exc_info is None
    assert caplog.records[0].levelno == logging.WARNING


# ------------------------------------------------------------------ statistics


def test_stats_sync(fake_clock: FakeClock) -> None:
    f = retry(Flaky(2), delay=2, max_attempts=3)
    stats = retry_stats(f)
    assert isinstance(stats, RetryStats)
    assert (stats.calls, stats.attempts_total) == (0, 0)
    f()
    assert (stats.calls, stats.attempts_total, stats.retries, stats.gave_up) == (1, 3, 2, 0)
    assert stats.total_sleep == 4
    assert isinstance(stats.last_exception, ValueError)
    assert "calls=1" in repr(stats)
    assert stats.snapshot()["retries"] == 2
    stats.reset()
    assert stats.snapshot()["calls"] == 0 and stats.last_exception is None


def test_stats_gave_up_and_non_matching(fake_clock: FakeClock) -> None:
    @retry(max_attempts=2, on=ValueError)
    def f(exc: type[BaseException]) -> None:
        raise exc

    with pytest.raises(ValueError):
        f(ValueError)
    with pytest.raises(KeyError):
        f(KeyError)
    stats = retry_stats(f)
    assert stats is not None
    assert (stats.calls, stats.attempts_total, stats.retries, stats.gave_up) == (2, 3, 1, 1)


def test_stats_async_methods_and_stacking(fake_clock: FakeClock) -> None:
    class C:
        n = 0

        @retry(max_attempts=3, delay=1)
        async def go(self) -> int:
            self.n += 1
            if self.n < 2:
                raise ValueError
            return self.n

    c = C()
    assert asyncio.run(c.go()) == 2
    stats = retry_stats(c.go)
    assert stats is retry_stats(C.go) and stats is not None
    assert stats.attempts_total == 2

    def outer(fn: Any) -> Any:
        @functools.wraps(fn)
        def w(*a: Any, **k: Any) -> Any:
            return fn(*a, **k)

        return w

    wrapped = outer(retry(Flaky(0)))
    assert retry_stats(wrapped) is not None
    assert retry_stats(len) is None
    assert retry_stats(lambda: 1) is None


def test_stats_thread_safe() -> None:
    @retry(max_attempts=2, delay=0)
    def f() -> None:
        raise ValueError

    def work() -> None:
        for _ in range(200):
            with contextlib.suppress(ValueError):
                f()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = retry_stats(f)
    assert stats is not None
    assert (stats.calls, stats.attempts_total, stats.retries, stats.gave_up) == (
        1600,
        3200,
        1600,
        1600,
    )


# ------------------------------------------------------------------ retrying


def test_retrying_sync_success_after_failures(fake_clock: FakeClock) -> None:
    f = Flaky(2)
    numbers = []
    result = None
    for attempt in retrying(max_attempts=3, delay=0.5, backoff=2):
        with attempt:
            numbers.append(attempt.number)
            result = f()
    assert result == "ok"
    assert numbers == [1, 2, 3]
    assert fake_clock.sleeps == [0.5, 1.0]


def test_retrying_final_failure_propagates(fake_clock: FakeClock) -> None:
    f = Flaky(10)
    with pytest.raises(ValueError, match="fail 2"):
        for attempt in retrying(max_attempts=2):
            with attempt:
                f()
    assert f.calls == 2


def test_retrying_non_matching_propagates_and_first_try_success(fake_clock: FakeClock) -> None:
    with pytest.raises(KeyError):
        for attempt in retrying(on=ValueError):
            with attempt:
                raise KeyError
    runs = 0
    for attempt in retrying():
        with attempt:
            runs += 1
    assert runs == 1
    assert fake_clock.sleeps == []


def test_retrying_options_shared_with_retry(fake_clock: FakeClock) -> None:
    states: list[RetryState] = []
    f = Flaky(100)
    with pytest.raises(ValueError):
        for attempt in retrying(
            max_attempts=None,
            delay=1,
            backoff=2,
            max_time=10,
            on_retry=states.append,
            retry_if=lambda e: True,
        ):
            with attempt:
                f()
    assert fake_clock.sleeps == [1, 2, 4]
    assert [s.attempt for s in states] == [1, 2, 3]

    g = Flaky(2)
    for attempt in retrying(delay_from=lambda e: 0.25, max_attempts=3):
        with attempt:
            g()
    assert fake_clock.sleeps[-2:] == [0.25, 0.25]

    with pytest.raises(ValueError):
        for attempt in retrying(retry_if=lambda e: False):
            with attempt:
                raise ValueError


def test_retrying_validation_and_reuse(fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        retrying(max_attempts=0)
    r = retrying(max_attempts=2, delay=0)
    for _ in range(2):  # independent runs
        f = Flaky(1)
        for attempt in r:
            with attempt:
                f()
    assert r.stats.calls == 2
    assert r.stats.retries == 2


def test_retrying_async(fake_clock: FakeClock) -> None:
    f = Flaky(2)

    async def main() -> str:
        out = ""
        async for attempt in retrying(max_attempts=3, delay=1):
            with attempt:
                out = f()
        return out

    assert asyncio.run(main()) == "ok"
    assert fake_clock.sleeps == [1, 1]


def test_retrying_async_failure_propagates(fake_clock: FakeClock) -> None:
    async def main() -> None:
        async for attempt in retrying(max_attempts=2):
            with attempt:
                raise ValueError("x")

    with pytest.raises(ValueError):
        asyncio.run(main())
    assert fake_clock.sleeps == [1.0]


# ------------------------------------------------------------------ overrides / events


def test_max_attempts_override_applies_at_call_time(fake_clock: FakeClock) -> None:
    f = Flaky(10)
    wrapped = retry(f, max_attempts=5)
    g = Flaky(10)
    previous = _core.set_overrides(max_attempts=2)
    try:
        with pytest.raises(ValueError):
            wrapped()
        assert f.calls == 2
        with pytest.raises(ValueError):
            for attempt in retrying(max_attempts=5):
                with attempt:
                    g()
        assert g.calls == 2
        h = Flaky(10)
        with pytest.raises(ValueError):
            retry(h, max_attempts=None)()
        assert h.calls == 2
        _core.set_overrides(max_attempts=10)
        k = Flaky(10)
        with pytest.raises(ValueError):
            retry(k, max_attempts=3)()
        assert k.calls == 3  # override only lowers
    finally:
        _core.set_overrides(**previous)


def test_events_emitted(fake_clock: FakeClock) -> None:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    try:
        retry(Flaky(1), delay=2)()
        with pytest.raises(ValueError):
            retry(Flaky(5), max_attempts=2, delay=1)()
        for attempt in retrying(name="block", delay=1):
            with attempt:
                pass
    finally:
        _core.listeners.remove(seen.append)
    names = [e.name for e in seen]
    assert names == [
        "retry.attempt_failed",
        "retry.succeeded",
        "retry.attempt_failed",
        "retry.gave_up",
        "retry.succeeded",
    ]
    assert seen[0].fields["attempt"] == 1 and seen[0].fields["next_delay"] == 2
    assert isinstance(seen[0].fields["exception"], ValueError)
    assert seen[1].fields == {"attempts": 2}
    assert seen[3].fields["attempts"] == 2
    assert seen[4].function == "block" and seen[4].fields == {"attempts": 1}


# ======================================================= budgets, results, async hooks


@pytest.fixture
def events() -> Iterator[list[_core.Event]]:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    try:
        yield seen
    finally:
        _core.listeners.remove(seen.append)


def always_fails(counter: list[int]) -> Any:
    def f() -> None:
        counter.append(1)
        raise ConnectionError("down")

    return f


# --------------------------------------------------------------------------- budget


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ratio": -0.1},
        {"ratio": float("nan")},
        {"ratio": float("inf")},
        {"min_per_second": -1},
        {"window": 0},
        {"window": float("inf")},
    ],
)
def test_budget_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetryBudget(**kwargs)


def test_budget_ratio_and_reserve(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0.5, min_per_second=0.2, window=10)  # reserve = 2 retries
    assert budget.stats == RetryBudgetStats(requests=0, retries=0, rejected=0, allowed=2)
    for _ in range(4):
        budget.deposit()
    assert budget.stats.allowed == 4  # 0.5 * 4 + 2
    assert [budget.try_withdraw() for _ in range(5)] == [True] * 4 + [False]
    assert budget.stats == RetryBudgetStats(requests=4, retries=4, rejected=1, allowed=0)
    assert "ratio=0.5" in repr(budget)


def test_budget_window_slides(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=1.0, min_per_second=0, window=10)
    budget.deposit()
    assert budget.try_withdraw()
    assert not budget.try_withdraw()
    fake_clock.advance(5)
    budget.deposit()
    assert budget.try_withdraw()
    assert budget.stats.requests == 2
    fake_clock.advance(6)  # the first deposit and retry are now older than the window
    assert budget.stats == RetryBudgetStats(requests=1, retries=1, rejected=1, allowed=0)
    fake_clock.advance(10)
    assert budget.stats.requests == 0
    budget.reset()
    assert budget.stats == RetryBudgetStats(0, 0, 0, 0)


def test_budget_exhaustion_stops_retrying_at_once(
    fake_clock: FakeClock, events: list[_core.Event]
) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0.1, window=10)  # one retry per window
    calls: list[int] = []
    f = retry(always_fails(calls), max_attempts=5, delay=1, budget=budget)
    with pytest.raises(ConnectionError):
        f()
    assert len(calls) == 2  # first attempt + the one retry the budget allowed
    assert fake_clock.sleeps == [1]  # no sleep after the refusal
    names = [e.name for e in events]
    assert names == ["retry.attempt_failed", "retry.budget_exhausted", "retry.gave_up"]
    assert events[1].fields["attempts"] == 2
    assert isinstance(events[1].fields["exception"], ConnectionError)
    stats = retry_stats(f)
    assert stats is not None
    assert (stats.retries, stats.gave_up, stats.budget_exhausted) == (1, 1, 1)
    assert stats.snapshot()["budget_exhausted"] == 1
    assert "budget_exhausted=1" in repr(stats)
    stats.reset()
    assert stats.budget_exhausted == 0
    assert budget.stats.rejected == 1


def test_budget_shared_between_functions_and_retrying(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0.2, window=10)  # two retries
    a_calls: list[int] = []
    b_calls: list[int] = []
    a = retry(always_fails(a_calls), max_attempts=3, delay=0, budget=budget)
    b = retry(always_fails(b_calls), max_attempts=3, delay=0, budget=budget)
    with pytest.raises(ConnectionError):
        a()
    with pytest.raises(ConnectionError):
        b()
    assert (len(a_calls), len(b_calls)) == (3, 1)
    tries: list[int] = []
    with pytest.raises(ConnectionError):
        for attempt in retrying(max_attempts=3, delay=0, budget=budget):
            with attempt:
                tries.append(attempt.number)
                raise ConnectionError
    assert tries == [1]
    assert budget.stats.requests == 3


def test_budget_does_not_touch_success_or_non_retryable(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0, window=1)

    @retry(budget=budget, on=ConnectionError)
    def ok() -> int:
        return 1

    @retry(budget=budget, on=ConnectionError)
    def wrong() -> None:
        raise KeyError("x")

    assert ok() == 1
    with pytest.raises(KeyError):
        wrong()
    assert budget.stats == RetryBudgetStats(requests=2, retries=0, rejected=0, allowed=0)


def test_budget_async(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0.1, window=10)
    calls: list[int] = []

    @retry(max_attempts=4, delay=0.5, budget=budget)
    async def f() -> None:
        calls.append(1)
        raise ConnectionError

    with pytest.raises(ConnectionError):
        asyncio.run(f())
    assert len(calls) == 2
    assert fake_clock.sleeps == [0.5]
    assert "retry.budget_exhausted" in [e.name for e in events]


def test_budget_thread_safe() -> None:
    budget = RetryBudget(ratio=0.5, min_per_second=0, window=1000)
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(200):
            budget.deposit()
            ok = budget.try_withdraw()
            ok2 = budget.try_withdraw()
            with lock:
                granted.extend([ok, ok2])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = budget.stats
    assert stats.requests == 1600
    assert sum(granted) == stats.retries == 800  # never more than ratio * requests
    assert stats.rejected == 3200 - 800


def test_budget_with_fast_mode_and_disable_retries() -> None:
    budget = RetryBudget(ratio=0, min_per_second=0.1, window=10)
    calls: list[int] = []
    f = retry(always_fails(calls), max_attempts=5, delay=60, budget=budget)
    with fast_mode(), pytest.raises(ConnectionError):
        f()
    assert len(calls) == 2
    budget.reset()
    calls.clear()
    with disable_retries(), pytest.raises(ConnectionError):
        f()
    assert len(calls) == 1
    assert budget.stats.retries == 0  # no retry was attempted, so nothing withdrawn


def test_budget_type_checked_and_configurable(fake_clock: FakeClock) -> None:
    with pytest.raises(TypeError, match="RetryBudget"):
        retry(budget="lots")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RetryBudget"):
        retrying(budget=3)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RetryBudget"):
        configure(retry={"budget": 0.1})
    budget = RetryBudget(ratio=0, min_per_second=0, window=1)
    with configured(retry={"budget": budget, "delay": 0}):
        calls: list[int] = []
        f = retry(always_fails(calls))
        g = retry(always_fails([]), budget=None)  # explicit None opts out
    with pytest.raises(ConnectionError):
        f()
    assert len(calls) == 1 and budget.stats.rejected == 1
    with pytest.raises(ConnectionError):
        g()
    assert budget.stats.requests == 1


# ------------------------------------------------------------------ retry_if_result


def test_retry_if_result_sync(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    replies = iter([None, None, 7])
    seen: list[RetryState] = []

    @retry(max_attempts=5, delay=1, retry_if_result=lambda r: r is None, on_retry=seen.append)
    def poll() -> int | None:
        return next(replies)

    assert poll() == 7
    assert fake_clock.sleeps == [1, 1]
    assert [(s.attempt, s.exception, s.result) for s in seen] == [(1, None, None), (2, None, None)]
    assert [e.name for e in events] == [
        "retry.attempt_failed",
        "retry.attempt_failed",
        "retry.succeeded",
    ]
    assert events[0].fields["exception"] is None
    assert events[2].fields == {"attempts": 3}


def test_retry_if_result_exhausted_returns_last_value(
    fake_clock: FakeClock, events: list[_core.Event], caplog: pytest.LogCaptureFixture
) -> None:
    replies = iter(["busy-1", "busy-2", "busy-3"])
    seen: list[RetryState] = []

    @retry(
        max_attempts=3,
        delay=0,
        retry_if_result=lambda r: r.startswith("busy"),
        on_retry=seen.append,
    )
    def status() -> str:
        return next(replies)

    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        assert status() == "busy-3"
    assert [s.result for s in seen] == ["busy-1", "busy-2"]
    assert "returned 'busy-1'" in caplog.text
    assert [e.name for e in events][-1] == "retry.gave_up"
    assert events[-1].fields == {"attempts": 3, "exception": None}
    stats = retry_stats(status)
    assert stats is not None
    assert (stats.calls, stats.attempts_total, stats.retries, stats.gave_up) == (1, 3, 2, 1)
    assert stats.last_exception is None


def test_retry_if_result_mixed_with_exceptions(fake_clock: FakeClock) -> None:
    replies: Iterator[Any] = iter([ValueError("x"), -1, 5])

    @retry(max_attempts=4, delay=0, on=ValueError, retry_if_result=lambda r: r < 0)
    def f() -> int:
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return int(value)

    assert f() == 5
    assert retry_stats(f).retries == 2  # type: ignore[union-attr]


def test_retry_if_result_predicate_error_propagates(fake_clock: FakeClock) -> None:
    def bad(_: object) -> bool:
        raise RuntimeError("predicate bug")

    with pytest.raises(RuntimeError, match="predicate bug"):
        retry(lambda: 1, retry_if_result=bad)()


def test_retry_if_result_budget_exhausted_returns_value(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0, window=1)
    calls: list[int] = []

    @retry(max_attempts=5, delay=0, retry_if_result=lambda r: r is None, budget=budget)
    def f() -> None:
        calls.append(1)

    assert f() is None
    assert len(calls) == 1
    assert retry_stats(f).budget_exhausted == 1  # type: ignore[union-attr]


def test_retry_if_result_under_disable_retries_and_fast_mode() -> None:
    calls: list[int] = []

    @retry(max_attempts=4, delay=30, retry_if_result=lambda r: r is None)
    def f() -> None:
        calls.append(1)

    with fast_mode():
        assert f() is None
    assert len(calls) == 4
    calls.clear()
    with disable_retries():
        f()
    assert len(calls) == 1


def test_retry_if_result_async_sync_and_async_predicates(fake_clock: FakeClock) -> None:
    async def is_none(value: object) -> bool:
        await asyncio.sleep(0)
        return value is None

    for predicate in (lambda v: v is None, is_none):
        replies = iter([None, "ok"])

        @retry(max_attempts=3, delay=0.25, retry_if_result=predicate)
        async def f() -> str | None:
            return next(replies)  # noqa: B023  # called inside this iteration

        assert asyncio.run(f()) == "ok"
    assert fake_clock.sleeps == [0.25, 0.25]


def test_retry_if_result_async_exhausted(fake_clock: FakeClock) -> None:
    seen: list[RetryState] = []

    async def note(state: RetryState) -> None:
        seen.append(state)

    @retry(max_attempts=2, delay=0, retry_if_result=lambda v: v == 0, on_retry=note)
    async def f() -> int:
        return 0

    assert asyncio.run(f()) == 0
    assert [s.result for s in seen] == [0]
    assert retry_stats(f).gave_up == 1  # type: ignore[union-attr]


def test_retrying_with_result_sync(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    replies = iter([None, None, "done"])
    value = None
    for attempt in retrying(max_attempts=5, delay=1, retry_if_result=lambda r: r is None):
        with attempt:
            value = next(replies)
            attempt.result = value
    assert value == "done" and attempt.result == "done"
    assert fake_clock.sleeps == [1, 1]
    assert [e.name for e in events][-1] == "retry.succeeded"

    events.clear()
    numbers = []
    for attempt in retrying(max_attempts=2, delay=0, retry_if_result=lambda r: r is None):
        with attempt:
            numbers.append(attempt.number)
            assert attempt.result is None  # unset reads as None
            attempt.result = None
    assert numbers == [1, 2]  # loop ended normally with the last (bad) value
    assert [e.name for e in events] == ["retry.attempt_failed", "retry.gave_up"]

    # Not setting attempt.result means the predicate is not consulted.
    for attempt in retrying(max_attempts=3, retry_if_result=lambda r: True):
        with attempt:
            numbers.append(0)
    assert numbers[-1] == 0 and len(numbers) == 3


def test_retrying_with_result_async(fake_clock: FakeClock) -> None:
    async def is_none(value: object) -> bool:
        return value is None

    async def main() -> list[int]:
        numbers: list[int] = []
        replies = iter([None, 1])
        async for attempt in retrying(max_attempts=3, delay=0.5, retry_if_result=is_none):
            async with attempt:
                numbers.append(attempt.number)
                attempt.result = next(replies)
        replies = iter([None, None])
        async for attempt in retrying(max_attempts=2, delay=0, retry_if_result=is_none):
            async with attempt:
                numbers.append(attempt.number)
                attempt.result = next(replies)
        return numbers

    assert asyncio.run(main()) == [1, 2, 1, 2]
    assert fake_clock.sleeps == [0.5, 0]


# ------------------------------------------------------------------------ async hooks


def test_async_retry_if_and_on_retry_are_awaited(fake_clock: FakeClock) -> None:
    seen: list[int] = []

    async def transient(exc: BaseException) -> bool:
        await asyncio.sleep(0)
        return "transient" in str(exc)

    async def note(state: RetryState) -> None:
        await asyncio.sleep(0)
        seen.append(state.attempt)

    errors = iter([ConnectionError("transient"), ConnectionError("transient")])

    @retry(max_attempts=5, delay=0, retry_if=transient, on_retry=note)
    async def f() -> str:
        err = next(errors, None)
        if err is not None:
            raise err
        return "ok"

    assert asyncio.run(f()) == "ok"
    assert seen == [1, 2]

    @retry(max_attempts=5, delay=0, retry_if=transient)
    async def fatal() -> None:
        raise ConnectionError("fatal")

    with pytest.raises(ConnectionError, match="fatal"):
        asyncio.run(fatal())
    assert retry_stats(fatal).attempts_total == 1  # type: ignore[union-attr]


def test_async_function_still_accepts_sync_hooks(fake_clock: FakeClock) -> None:
    seen: list[int] = []
    errors = iter([ValueError("a")])

    @retry(delay=0, retry_if=lambda e: True, on_retry=lambda s: seen.append(s.attempt))
    async def f() -> int:
        err = next(errors, None)
        if err is not None:
            raise err
        return 1

    assert asyncio.run(f()) == 1
    assert seen == [1]


@pytest.mark.parametrize("hook", ["retry_if", "retry_if_result", "on_retry"])
def test_sync_function_rejects_async_hooks_at_decoration(hook: str) -> None:
    async def cb(_: object) -> bool:
        return True

    deco = retry(**{hook: cb})
    with pytest.raises(TypeError, match=f"{hook}= is an async function"):

        @deco
        def f() -> None: ...


def test_retrying_async_hooks_need_async_with(fake_clock: FakeClock) -> None:
    async def yes(_: object) -> bool:
        return True

    with pytest.raises(TypeError, match="async with attempt"):
        for attempt in retrying(retry_if=yes, delay=0):
            with attempt:
                raise ValueError
    with pytest.raises(TypeError, match="retry_if_result="):
        for attempt in retrying(retry_if_result=yes, delay=0):
            with attempt:
                attempt.result = 1

    async def main() -> list[int]:
        seen: list[int] = []

        async def note(state: RetryState) -> None:
            seen.append(state.attempt)

        async for attempt in retrying(max_attempts=3, delay=0, retry_if=yes, on_retry=note):
            async with attempt:
                if attempt.number < 3:
                    raise ValueError
        with pytest.raises(KeyError):
            async for attempt in retrying(on=ValueError, retry_if=yes):
                async with attempt:
                    raise KeyError
        with pytest.raises(ValueError):
            async for attempt in retrying(max_attempts=2, delay=0, retry_if=yes):
                async with attempt:
                    raise ValueError
        return seen

    assert asyncio.run(main()) == [1, 2]


def test_async_hooks_do_not_retry_cancellation(fake_clock: FakeClock) -> None:
    async def yes(_: object) -> bool:
        return True

    @retry(on=BaseException, retry_if=yes, delay=0)
    async def f() -> None:
        raise asyncio.CancelledError

    async def main() -> None:
        with pytest.raises(asyncio.CancelledError):
            await f()
        with pytest.raises(asyncio.CancelledError):
            async for attempt in retrying(on=BaseException, retry_if=yes):
                async with attempt:
                    raise asyncio.CancelledError

    asyncio.run(main())
    assert retry_stats(f).attempts_total == 1  # type: ignore[union-attr]
