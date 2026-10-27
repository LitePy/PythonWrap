"""Tests for python_wrap.policy."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest

from python_wrap import CircuitBreaker, CircuitOpenError, retry
from python_wrap.policy import ROLES, Policy, policy


def _rec(log: list[str], label: str) -> Callable[[Any], Any]:
    def decorator(func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            log.append(label)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def test_order_is_canonical_regardless_of_keyword_order() -> None:
    log: list[str] = []
    p = policy(
        timeout=_rec(log, "timeout"),
        retry=_rec(log, "retry"),
        fallback=_rec(log, "fallback"),
        breaker=_rec(log, "breaker"),
        inner=_rec(log, "inner"),
        outer=_rec(log, "outer"),
    )

    @p
    def f() -> int:
        log.append("body")
        return 1

    assert f() == 1
    assert log == ["outer", "fallback", "breaker", "retry", "timeout", "inner", "body"]
    assert p.order == ("outer", "fallback", "breaker", "retry", "timeout", "inner")


def test_all_roles_in_roles_order() -> None:
    log: list[str] = []
    p = policy(**{role: _rec(log, role) for role in reversed(ROLES)})
    p.apply(lambda: None)()
    assert log == list(ROLES)


def test_lists_keep_given_order_within_role() -> None:
    log: list[str] = []
    p = policy(observe=[_rec(log, "a"), _rec(log, "b")])
    p(lambda: None)()
    assert log == ["a", "b"]


def test_real_decorators_breaker_outside_retry() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60, on=ConnectionError)
    calls = 0
    p = policy(
        retry=retry(max_attempts=3, delay=0, on=ConnectionError),
        breaker=breaker,
        name="x",
    )

    @p
    def f() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError

    with pytest.raises(ConnectionError):
        f()
    assert calls == 3
    with pytest.raises(CircuitOpenError):
        f()


def test_async_function_stays_async() -> None:
    @policy(retry=retry(max_attempts=2, delay=0))
    async def f(x: int) -> int:
        return x + 1

    assert inspect.iscoroutinefunction(f)
    assert asyncio.run(f(1)) == 2


def test_methods_static_and_class() -> None:
    log: list[str] = []
    p = policy(retry=_rec(log, "r"))

    class C:
        @p
        def m(self, x: int) -> int:
            return x

        @p
        @staticmethod
        def s(x: int) -> int:
            return x

        @p
        @classmethod
        def c(cls, x: int) -> int:
            return x

    assert C().m(1) == 1
    assert C.s(2) == 2
    assert C.c(3) == 3
    assert isinstance(C.__dict__["s"], staticmethod)
    assert isinstance(C.__dict__["c"], classmethod)
    assert log == ["r", "r", "r"]


def test_immutable_and_with_derives_variants() -> None:
    log: list[str] = []
    base = policy(retry=_rec(log, "r"), timeout=_rec(log, "t"), name="base")
    derived = base.with_(retry=None, fallback=_rec(log, "f"), name="d")
    assert base.order == ("retry", "timeout")
    assert derived.order == ("fallback", "timeout")
    assert derived.name == "d"
    assert base.with_(outer=_rec(log, "o")).order == ("outer", "retry", "timeout")
    assert base.with_(inner=_rec(log, "i")).order[-1] == "inner"
    with pytest.raises(AttributeError):
        base.name = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del base._name


def test_repr_lists_order() -> None:
    p = policy(timeout=_rec([], "t"), retry=_rec([], "r"), name="payments")
    assert repr(p) == "Policy('payments', retry > timeout)"
    assert repr(policy()) == "Policy((empty))"
    assert policy().name is None
    assert isinstance(p, Policy)


def test_validation_errors() -> None:
    with pytest.raises(TypeError, match="retry="):
        policy(retry=3)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unknown"):
        policy().with_(bogus=1)


def test_empty_policy_is_identity() -> None:
    def f() -> int:
        return 1

    assert policy()(f) is f
