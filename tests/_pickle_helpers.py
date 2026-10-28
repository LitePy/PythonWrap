"""Module-level decorated functions for ``test_pickling.py``.

They live in their own importable module because pickle stores functions by reference
(``module.qualname``): a process started with the "spawn" method re-imports this module
by name to find them.
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

import python_wrap as pw

breaker = pw.CircuitBreaker(failure_threshold=3, name="pickle-helpers")
bulkhead = pw.Bulkhead(4)
shared_policy = pw.policy(retry=pw.retry(max_attempts=2, delay=0), timeout=pw.timeout(5))


@pw.retry(max_attempts=2, delay=0)
def f_retry(x: int) -> int:
    return x + 1


@pw.fallback(-1)
def f_fallback(x: int) -> int:
    return x + 1


@pw.suppress(ValueError)
def f_suppress(x: int) -> int:
    return x + 1


@pw.circuit_breaker(failure_threshold=3)
def f_circuit_breaker(x: int) -> int:
    return x + 1


@breaker
def f_breaker_object(x: int) -> int:
    return x + 1


@pw.timeout(5)
def f_timeout(x: int) -> int:
    return x + 1


@pw.memoize
def f_memoize(x: int) -> int:
    return x + 1


@pw.memoize(maxsize=8, ttl=60)
def f_memoize_options(x: int) -> int:
    return x + 1


@pw.cache(60)
def f_cache(x: int) -> int:
    return x + 1


@pw.once
def f_once(x: int) -> int:
    return x + 1


@pw.rate_limit(100, 1)
def f_rate_limit(x: int) -> int:
    return x + 1


@pw.throttle(0.001)
def f_throttle(x: int) -> int:
    return x + 1


@pw.debounce(0.01)
def f_debounce(x: int) -> int:
    return x + 1


@pw.singleflight
def f_singleflight(x: int) -> int:
    return x + 1


@pw.run_in_thread
def f_run_in_thread(x: int) -> int:
    return x + 1


@pw.limit_concurrency(4)
def f_limit_concurrency(x: int) -> int:
    return x + 1


@bulkhead
def f_bulkhead(x: int) -> int:
    return x + 1


@pw.batch_calls(max_size=10, max_wait=0.001)
def f_batch_calls(xs: list[int]) -> list[int]:
    return [x + 1 for x in xs]


@pw.hedge(5.0)
def f_hedge(x: int) -> int:
    return x + 1


@pw.periodic(60)
def f_periodic(x: int) -> int:
    return x + 1


@pw.log_args
def f_log_args(x: int) -> int:
    return x + 1


@pw.log_return
def f_log_return(x: int) -> int:
    return x + 1


@pw.audit
def f_audit(x: int) -> int:
    return x + 1


@pw.trace
def f_trace(x: int) -> int:
    return x + 1


@pw.timing
def f_timing(x: int) -> int:
    return x + 1


@pw.measure_memory
def f_measure_memory(x: int) -> int:
    return x + 1


@pw.profile
def f_profile(x: int) -> int:
    return x + 1


@pw.validate_args(x=lambda v: v >= 0)
def f_validate_args(x: int) -> int:
    return x + 1


@pw.check_type(x=int)
def f_check_type(x: int) -> int:
    return x + 1


@pw.enforce_types
def f_enforce_types(x: int) -> int:
    return x + 1


@pw.revert_on_failure
def f_revert_on_failure(x: int) -> int:
    return x + 1


@pw.transactional
def f_transactional(x: int) -> int:
    return x + 1


@pw.deprecate
def f_deprecate(x: int) -> int:
    return x + 1


@pw.mock_data(None, enabled=False)
def f_mock_data(x: int) -> int:
    return x + 1


@pw.compose(pw.timing, pw.retry(max_attempts=2, delay=0))
def f_compose(x: int) -> int:
    return x + 1


with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)  # no_debug warns when applied

    @pw.no_debug
    def f_no_debug(x: int) -> int:
        return x + 1


@pw.count_calls
def f_count_calls(x: int) -> int:
    return x + 1


@shared_policy
def f_policy(x: int) -> int:
    return x + 1


@pw.memoize
@pw.retry(max_attempts=2, delay=0)
@pw.count_calls
def f_stacked(x: int) -> int:
    return x + 1


@pw.retry(max_attempts=2, delay=0)
async def af_retry(x: int) -> int:
    return x + 1


@pw.memoize
async def af_memoize(x: int) -> int:
    return x + 1


@pw.periodic(60)
async def af_periodic(x: int) -> int:
    return x + 1


@pw.debounce(0.01)
async def af_debounce(x: int) -> int:
    return x + 1


@pw.batch_calls(max_size=10, max_wait=0.001)
async def af_batch_calls(xs: list[int]) -> list[int]:
    return [x + 1 for x in xs]


@pw.wrap_methods(pw.retry(max_attempts=2, delay=0), pw.count_calls)
class Service:
    """A class whose methods were decorated by `wrap_methods`."""

    def __init__(self, base: int = 0) -> None:
        self.base = base

    def add(self, x: int) -> int:
        return self.base + x


class Model:
    """An instance that uses `cached_property`, `memoize` methods and decorated methods."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.computed = 0

    @pw.cached_property
    def square(self) -> int:
        self.computed += 1
        return self.n * self.n

    @pw.memoize
    def times(self, k: int) -> int:
        return self.n * k

    @pw.memoize(per_instance=True)
    def plus(self, k: int) -> int:
        return self.n + k

    @pw.retry(max_attempts=2, delay=0)
    def retried(self, k: int) -> int:
        return self.n - k

    @pw.count_calls
    def counted(self) -> int:
        return self.n


def call_in_child(func: Any, *args: Any) -> Any:
    """Run in a worker process: call a function that arrived there by pickle."""
    return func(*args)


def run_async(func: Any, *args: Any) -> Any:
    """Run in a worker process: await an ``async def`` function that arrived by pickle."""
    return asyncio.run(func(*args))


def count_calls_seen() -> int:
    """Run in a worker process: how many calls this process's counter has seen."""
    return f_count_calls.stats.calls
