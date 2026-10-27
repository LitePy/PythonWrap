"""Type-checking cases (not collected by pytest); verified by tests/test_typing.py.

Every line must type-check under ``mypy --strict``. Lines ending in ``# type: ignore[...]``
are deliberate mistakes: if a regression stops the checker from flagging one, mypy
reports the ignore as unused and the test fails. ``assert_type`` pins inferred types.

The file is also clean under pyright strict (with ``reportUnnecessaryTypeIgnoreComment``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from python_wrap import (
    CircuitBreaker,
    audit,
    check_type,
    circuit_breaker,
    debounce,
    enforce_types,
    log_args,
    log_return,
    measure_memory,
    memoize,
    mock_data,
    once,
    profile,
    rate_limit,
    retry,
    revert_on_failure,
    singleflight,
    timeout,
    timing,
    trace,
    transactional,
    validate_args,
)
from python_wrap.caching import CacheInfo, Memoized, Once
from python_wrap.circuit import CircuitProtected
from python_wrap.concurrency import AsyncDebounced, Debounced

if TYPE_CHECKING:
    from typing_extensions import assert_type

T = TypeVar("T")

# --- 1. pass-through decorators keep the exact type, so inner attributes survive stacking


@retry(max_attempts=4, delay=0.01, on=ConnectionError)
@memoize(ttl=60, maxsize=256)
@rate_limit(10, 1.0, block=True)
@timeout(5.0)
def fetch_user(user_id: int) -> dict[str, int]:
    return {"id": user_id}


assert_type(fetch_user(1), dict[str, int])
assert_type(fetch_user.cache_info(), CacheInfo)
fetch_user("a")  # type: ignore[arg-type]
fetch_user.no_such_attribute  # type: ignore[attr-defined]  # noqa: B018

breaker = CircuitBreaker(failure_threshold=2)


def plain(x: int) -> str:
    return str(x)


m = memoize(plain)
PASS_THROUGH: list[Memoized[[int], str]] = [
    retry(m),
    retry(m, max_attempts=3),
    retry(max_attempts=2, delay=0)(m),
    timeout(1.0)(m),
    rate_limit(5, 1.0)(m),
    singleflight(m),
    singleflight(key=None)(m),
    log_args(m),
    log_args(redact=["pw"])(m),
    log_return(m),
    log_return(max_length=10)(m),
    audit(m),
    audit(sink=print)(m),
    trace(m),
    trace(logger="x")(m),
    timing(m),
    timing(threshold=0.1)(m),
    profile(m),
    profile(sort="time")(m),
    measure_memory(m),
    measure_memory(level=10)(m),
    enforce_types(m),
    enforce_types(check_return=False)(m),
    validate_args(x=lambda v: v > 0)(m),
    check_type(x=int)(m),
    revert_on_failure(m),
    revert_on_failure(target=0)(m),
    transactional(m),
    transactional(logger="x")(m),
    mock_data("fixed")(m),
    breaker(m),
]


@retry
def ident(x: T) -> T:
    return x


@timeout(1)
def ident_t(x: T) -> T:
    return x


assert_type(ident("s"), str)
assert_type(ident_t(1), int)


@retry(max_attempts=2, delay=0)
@timeout(1.0)
@breaker
async def fetch_async(x: int) -> list[int]:
    return [x]


async def use_async() -> None:
    assert_type(await fetch_async(1), list[int])
    await fetch_async("x")  # type: ignore[arg-type]


r_call = retry(plain, max_attempts=3)
assert_type(r_call(1), str)
r_call(1).no_such_method()  # type: ignore[attr-defined]

# --- 2/3. Protocol wrappers bind self (methods) and cls (classmethod stacked above).
# Class access returns a two-form callable (with and without the first argument), so
# mistakes there are reported as call-overload errors.


class Service:
    @memoize
    def meth(self, x: int) -> int:
        return x

    @classmethod
    @memoize
    def cm(cls, x: int) -> str:
        return str(x)

    @classmethod
    @once
    def setup(cls) -> int:
        return 1

    @once
    def om(self) -> int:
        return 1

    @debounce(0.1)
    def dm(self, x: int) -> None: ...

    @classmethod
    @debounce(0.1)
    def dcm(cls, x: int) -> None: ...

    @debounce(0.1)
    async def adm(self, x: int) -> str:
        return str(x)

    @classmethod
    @debounce(0.1)
    async def adcm(cls, x: int) -> str:
        return str(x)

    @circuit_breaker(failure_threshold=3)
    def cbm(self, x: int) -> int:
        return x

    @classmethod
    @circuit_breaker
    def cbcm(cls, x: int) -> int:
        return x


class Helpers:
    @staticmethod
    @memoize
    def st(x: int) -> int:
        return x

    @memoize
    def meth(self, x: int) -> int:
        return x


assert_type(Helpers.st(1), int)
assert_type(Helpers.meth(Helpers(), 1), int)
assert_type(Helpers.meth.cache_info(), CacheInfo)
Helpers.st("x")  # type: ignore[call-overload]

svc = Service()
assert_type(svc.meth(1), int)
assert_type(svc.meth.cache_info(), CacheInfo)
assert_type(Service.cm(1), str)
assert_type(svc.cm(1), str)
assert_type(Service.cm.cache_info(), CacheInfo)
assert_type(Service.setup(), int)
assert_type(Service.setup.called, bool)
assert_type(svc.om(), int)
svc.om.reset()
svc.dm(1)
svc.dm.cancel()
Service.dcm(1)
Service.dcm.cancel()
assert_type(Service.dcm.flush(), None)
assert_type(svc.cbm(1), int)
assert_type(svc.cbm.breaker, CircuitBreaker)
assert_type(Service.cbcm(1), int)
assert_type(Service.cbcm.breaker, CircuitBreaker)


async def use_async_methods() -> None:
    assert_type(await svc.adm(1), str)
    assert_type(await Service.adcm(1), str)
    assert_type(await svc.adm.flush(), str | None)
    svc.adm.cancel()


svc.meth("x")  # type: ignore[arg-type]
svc.meth()  # type: ignore[call-arg]
Service.cm("x")  # type: ignore[call-overload]
Service.cm()  # type: ignore[call-overload]
Service.setup(1)  # type: ignore[call-overload]
svc.dm("x")  # type: ignore[arg-type]
svc.dm()  # type: ignore[call-arg]
Service.dcm("x")  # type: ignore[call-overload]
svc.dm.no_such_method()  # type: ignore[attr-defined]
svc.cbm("x")  # type: ignore[arg-type]
Service.cbcm("x")  # type: ignore[call-overload]

# --- 4. circuit_breaker exposes .breaker on both overloads


@circuit_breaker(failure_threshold=3)
def cb_param(a: int) -> int:
    return a


@circuit_breaker
def cb_bare(a: int) -> int:
    return a


@circuit_breaker
async def cb_async(a: int) -> int:
    return a


assert_type(cb_param(1), int)
assert_type(cb_param.breaker, CircuitBreaker)
assert_type(cb_bare.breaker, CircuitBreaker)
assert_type(cb_async.breaker, CircuitBreaker)
protected: CircuitProtected[[int], int] = cb_bare
cb_param("x")  # type: ignore[arg-type]
cb_bare.breaker = breaker  # type: ignore[misc]

# The exported Protocols can annotate decorated functions.
memoized: Memoized[[int], str] = memoize(plain)
first: Once[[int], str] = once(plain)
debounced: Debounced[[int], str] = debounce(0.1)(plain)


async def coro(x: int) -> str:
    return str(x)


adebounced: AsyncDebounced[[int], str] = debounce(0.1)(coro)
callback: Callable[[int], Any] = memoized
