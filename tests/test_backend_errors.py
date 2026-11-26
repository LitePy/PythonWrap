"""One failure rule for shared backends: on_backend_error (raise | local | allow)."""

from __future__ import annotations

import pytest

from python_wrap.circuit import CircuitBreaker
from python_wrap.config import configured
from python_wrap.events import listening
from python_wrap.health import health
from python_wrap.quotas import RedisWindow
from python_wrap.shared import RedisBreakerState


class Dead:
    def eval(self, *args: object) -> object:
        raise ConnectionError("store down")

    get = eval


def test_local_fallback_emits_one_event_and_degrades_health() -> None:
    events: list[str] = []
    with listening(lambda e: events.append(e.name)):
        breaker = CircuitBreaker(
            name="g1-local", backend=RedisBreakerState(Dead()), on_backend_error="local"
        )

        @breaker
        def call() -> int:
            return 1

        assert [call(), call(), call()] == [1, 1, 1]
    assert events.count("backend.error") == 1  # later calls skip the dead store
    assert health.summary()["status"] == "degraded"


def test_raise_is_the_default_and_configure_changes_it() -> None:
    breaker = CircuitBreaker(name="g1-raise", backend=RedisBreakerState(Dead()))
    with pytest.raises(ConnectionError):
        breaker.call(lambda: 1)
    with configured(backend_errors="allow"):
        allowed = CircuitBreaker(name="g1-cfg", backend=RedisBreakerState(Dead()))
        assert allowed.call(lambda: 2) == 2


def test_window_local_fallback_enforces_limit_in_process() -> None:
    window = RedisWindow(Dead(), "g1-win", calls=2, period=60, on_backend_error="local")
    assert [window.acquire() for _ in range(2)] == [0, 0]
    assert window.acquire() > 0
    allow = RedisWindow(Dead(), "g1-allow", calls=1, period=60, on_backend_error="allow")
    assert allow.acquire() == 0


def test_bad_mode_rejected() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="g1-bad", backend=RedisBreakerState(Dead()), on_backend_error="x")
