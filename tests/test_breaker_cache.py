"""Shared breaker state cache: closed state served locally, batched successes, EVALSHA."""

from __future__ import annotations

from typing import Any

import pytest

from python_wrap.circuit import CircuitBreaker
from python_wrap.errors import CircuitOpenError
from python_wrap.shared import RedisBreakerState
from python_wrap.testing import fake_clock


def row(state: str = "closed", admitted: int = 1, successes: int = 0) -> list[Any]:
    return [admitted, state, 1, 0, 1000, 0, 0, successes, 0, 0, state]


class Counting:
    """A sync Redis stand-in: counts commands and answers with ``self.answer``."""

    def __init__(self) -> None:
        self.answer, self.commands, self.success_counts = row(), 0, []

    def eval(self, script: str, numkeys: int, *args: Any) -> list[Any]:
        self.commands += 1
        if args[2] == "success":
            self.success_counts.append(args[-1])
        return self.answer


def test_closed_breaker_needs_few_store_commands() -> None:
    client = Counting()
    breaker = CircuitBreaker(name="hot", backend=RedisBreakerState(client, state_ttl=1))
    with fake_clock():
        for _ in range(1000):
            assert breaker.call(lambda: 1) == 1
    assert client.commands < 10
    assert sum(client.success_counts) == 1000  # every success reached the store, in batches


def test_breaker_opened_elsewhere_rejects_within_state_ttl() -> None:
    client = Counting()
    breaker = CircuitBreaker(name="peer", backend=RedisBreakerState(client, state_ttl=1))
    with fake_clock() as clock:
        breaker.call(lambda: 1)
        client.answer = row("open", admitted=0)  # another process opened it
        assert breaker.call(lambda: 1) == 1  # still inside state_ttl: the documented trade
        clock.advance(1.0)
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: 1)


def test_failure_goes_to_store_after_pending_successes() -> None:
    client = Counting()
    state = RedisBreakerState(client, state_ttl=10, flush_interval=10)
    breaker = CircuitBreaker(name="mixed", backend=state)
    with fake_clock():
        for _ in range(3):
            breaker.call(lambda: 1)
        assert breaker.stats.successes == 3  # pending successes are counted in stats
        before = client.commands

        def boom() -> None:
            raise ValueError

        with pytest.raises(ValueError):
            breaker.call(boom)
    assert client.commands == before + 2  # flush of 3 successes, then the failure
    assert client.success_counts == [3]


def test_state_ttl_zero_sends_every_call() -> None:
    client = Counting()
    breaker = CircuitBreaker(name="every", backend=RedisBreakerState(client, state_ttl=0))
    for _ in range(5):
        breaker.call(lambda: 1)
    assert client.commands == 10


def test_evalsha_reloads_script_on_noscript() -> None:
    class NoScriptError(Exception):
        pass

    class Sha(Counting):
        loaded = False

        def evalsha(self, sha: str, numkeys: int, *args: Any) -> list[Any]:
            if not self.loaded:
                raise NoScriptError("No matching script. Please use EVAL.")
            self.commands += 1
            return self.answer

        def eval(self, script: str, numkeys: int, *args: Any) -> list[Any]:
            self.loaded = True
            return super().eval(script, numkeys, *args)

    client = Sha()
    state = RedisBreakerState(client, state_ttl=0)
    breaker = CircuitBreaker(name="sha", backend=state)
    breaker.call(lambda: 1)
    breaker.call(lambda: 1)
    assert client.loaded and client.commands == 4


def test_breaker_state_validates_options() -> None:
    for bad in ({"state_ttl": -1}, {"flush_interval": float("nan")}, {"flush_every": 0}):
        with pytest.raises(ValueError):
            RedisBreakerState(Counting(), **bad)
