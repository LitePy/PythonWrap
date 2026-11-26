"""Failure-rate and slow-call rules for CircuitBreaker, locally and in SQLite."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import pytest

from python_wrap import events
from python_wrap.circuit import CircuitBreaker, CircuitState, circuit_breaker
from python_wrap.local_state import SQLiteBreakerState
from python_wrap.testing import fake_clock


def run(
    breaker: CircuitBreaker, outcomes: list[bool], cost: Callable[[], None] = lambda: None
) -> None:
    """Call through ``breaker``: True succeeds, False fails with ConnectionError."""
    for ok in outcomes:

        def call(ok: bool = ok) -> None:
            cost()
            if not ok:
                raise ConnectionError

        with contextlib.suppress(ConnectionError):
            breaker.call(call)


@pytest.fixture(params=["local", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    if request.param == "local":
        yield None
        return
    state = SQLiteBreakerState(tmp_path / "breakers.db")
    yield state
    state.close()


def test_failure_rate_needs_volume_and_share(backend: Any) -> None:
    options = {"failure_rate": 0.5, "minimum_calls": 20, "window": 10, "backend": backend}
    with fake_clock():
        quiet = CircuitBreaker(name="quiet", **options)
        run(quiet, [False] * 5 + [True] * 95)
        assert quiet.state is CircuitState.CLOSED
        assert quiet.stats.failure_rate == pytest.approx(0.05)

        busy = CircuitBreaker(name="busy", **options)
        run(busy, [True] * 5 + [False] * 15)
        assert busy.state is CircuitState.OPEN


def test_rate_ignores_calls_older_than_window(backend: Any) -> None:
    with fake_clock() as clock:
        breaker = CircuitBreaker(
            name="aging", failure_rate=0.5, minimum_calls=4, window=10, backend=backend
        )
        run(breaker, [False, False, False])
        clock.advance(11)  # those failures left the window
        run(breaker, [True, True, True, False])
        assert breaker.state is CircuitState.CLOSED


def test_slow_successes_open_the_breaker(backend: Any) -> None:
    with fake_clock() as clock:
        breaker = CircuitBreaker(
            name="slowpoke",
            slow_call_duration=1.0,
            slow_call_rate=0.5,
            minimum_calls=4,
            window=60,
            recovery_timeout=5,
            backend=backend,
        )
        run(breaker, [True, True], cost=lambda: clock.advance(0.1))
        run(breaker, [True], cost=lambda: clock.advance(2))
        assert breaker.state is CircuitState.CLOSED
        run(breaker, [True], cost=lambda: clock.advance(2))  # 2 of 4 slow
        assert breaker.state is CircuitState.OPEN
        assert breaker.stats.slow_rate == pytest.approx(0.5)

        clock.advance(5)
        run(breaker, [True], cost=lambda: clock.advance(2))  # slow trial re-opens
        assert breaker.state is CircuitState.OPEN
        clock.advance(5)
        run(breaker, [True])  # a fast trial closes it
        assert breaker.state is CircuitState.CLOSED


def test_state_change_event_carries_rates() -> None:
    seen: list[dict[str, Any]] = []
    unsubscribe = events.subscribe(
        lambda e: seen.append(dict(e.fields)) if e.name == "circuit.state_changed" else None
    )
    try:
        with fake_clock():
            breaker = CircuitBreaker(name="ev", failure_rate=0.5, minimum_calls=2, window=10)
            run(breaker, [False, False])
    finally:
        unsubscribe()
    assert seen and seen[0]["failure_rate"] == 1.0 and seen[0]["slow_rate"] == 0.0


def test_plain_breaker_stats_have_no_rates() -> None:
    stats = CircuitBreaker(name="plain").stats
    assert stats.failure_rate is None and stats.slow_rate is None


@pytest.mark.parametrize(
    "options",
    [
        {"failure_rate": 0.5, "failure_threshold": 3, "window": 10},
        {"failure_rate": 0.5},
        {"failure_rate": 1.5, "window": 10},
        {"failure_rate": 0.5, "window": 10, "minimum_calls": 0},
        {"slow_call_duration": 1.0, "window": 10},
        {"slow_call_rate": 0.5, "window": 10},
        {"slow_call_duration": -1.0, "slow_call_rate": 0.5, "window": 10},
    ],
)
def test_invalid_rate_options(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(**options)


def test_decorator_accepts_rate_options() -> None:
    @circuit_breaker(failure_rate=0.5, minimum_calls=2, window=10)
    def boom() -> None:
        raise ConnectionError

    with fake_clock():
        for _ in range(2):
            with pytest.raises(ConnectionError):
                boom()
    assert boom.breaker.state is CircuitState.OPEN
