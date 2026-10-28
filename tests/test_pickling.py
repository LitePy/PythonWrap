"""Decorated functions pickle by reference and run in spawn-context process pools.

Per-process state holders (breakers, limiters, bulkheads, budgets, transactions, fake
clocks, policies) refuse to pickle with a clear ``TypeError``.
"""

from __future__ import annotations

import concurrent.futures
import copy
import functools
import multiprocessing
import os
import pickle
import site
from typing import Any

import pytest

import _pickle_helpers as h
import python_wrap as pw
from python_wrap.testing import FakeClock

DECORATED = sorted(
    name for name in vars(h) if name.startswith(("f_", "af_")) and callable(getattr(h, name))
)


@pytest.mark.parametrize("name", DECORATED)
@pytest.mark.parametrize("protocol", [2, pickle.DEFAULT_PROTOCOL, pickle.HIGHEST_PROTOCOL])
def test_decorated_function_pickles_by_reference(name: str, protocol: int) -> None:
    func = getattr(h, name)
    data = pickle.dumps(func, protocol=protocol)
    assert pickle.loads(data) is func
    assert len(data) < 200, "pickled by value instead of by reference"


def test_audit_covers_every_kind_of_wrapper() -> None:
    # The objects returned by periodic are not functions; they need __reduce__.
    assert isinstance(h.f_periodic, pw.Periodic)
    assert isinstance(h.af_periodic, pw.AsyncPeriodic)
    assert len(DECORATED) >= 40


def test_periodic_running_state_stays_in_its_process() -> None:
    assert pickle.loads(pickle.dumps(h.f_periodic)) is h.f_periodic
    assert copy.copy(h.f_periodic) is h.f_periodic  # copy also goes through __reduce__
    assert not h.f_periodic.running


@pytest.mark.parametrize("cls", [h.Service, h.Model])
def test_classes_with_decorated_methods_pickle_by_reference(cls: type) -> None:
    assert pickle.loads(pickle.dumps(cls)) is cls
    for attr in ("add",) if cls is h.Service else ("times", "plus", "retried", "counted"):
        method = getattr(cls, attr)
        assert pickle.loads(pickle.dumps(method)) is method


def test_wrap_methods_instance_and_bound_method_pickle() -> None:
    svc = h.Service(10)
    assert svc.add(1) == 11
    clone = pickle.loads(pickle.dumps(svc))
    assert clone.base == 10 and clone.add(2) == 12
    bound = pickle.loads(pickle.dumps(svc.add))
    assert bound(3) == 13


def test_instance_with_cached_property_and_memoized_methods_pickles() -> None:
    m = h.Model(4)
    assert m.square == 16 and m.computed == 1
    assert m.times(2) == 8 and m.plus(1) == 5 and m.retried(1) == 3 and m.counted() == 4

    clone = pickle.loads(pickle.dumps(m))
    assert clone.n == 4
    # cached_property values (and their locks) are not pickled: the copy recomputes.
    assert clone.computed == 1
    assert clone.square == 16 and clone.computed == 2
    assert clone.square == 16 and clone.computed == 2
    assert m.square == 16 and m.computed == 1  # the original keeps its cached value

    for attr, arg, expected in [("times", 3, 12), ("plus", 3, 7), ("retried", 3, 1)]:
        bound = pickle.loads(pickle.dumps(getattr(m, attr)))
        assert bound(arg) == expected
    assert pickle.loads(pickle.dumps(m.counted))() == 4


def test_instance_pickles_before_cached_property_is_used() -> None:
    clone = pickle.loads(pickle.dumps(h.Model(3)))
    assert clone.square == 9 and clone.computed == 1


@pytest.mark.parametrize(
    "make",
    [
        lambda: pw.CircuitBreaker(),
        lambda: h.breaker,
        lambda: pw.Bulkhead(2),
        lambda: pw.RateLimiter(5, 1),
        lambda: pw.limiter_of(h.f_rate_limit),
        lambda: pw.RetryBudget(),
        lambda: pw.Transaction(),
        lambda: FakeClock(),
    ],
    ids=[
        "breaker",
        "module-breaker",
        "bulkhead",
        "rate-limiter",
        "limiter_of",
        "budget",
        "transaction",
        "fake-clock",
    ],
)
def test_per_process_state_refuses_to_pickle(make: Any) -> None:
    obj = make()
    name = type(obj).__name__
    with pytest.raises(TypeError, match=f"cannot pickle '{name}' object: .*one process"):
        pickle.dumps(obj)
    with pytest.raises(TypeError, match="one process"):
        copy.deepcopy(obj)


def test_policy_refuses_to_pickle_but_decorated_functions_do() -> None:
    with pytest.raises(TypeError, match="cannot pickle a Policy"):
        pickle.dumps(h.shared_policy)
    assert pickle.loads(pickle.dumps(h.f_policy)) is h.f_policy


def test_refuse_pickle_advice_mentions_decorated_functions() -> None:
    with pytest.raises(TypeError, match="Create one Bulkhead per process") as info:
        pickle.dumps(pw.Bulkhead(1))
    assert "pickle by reference" in str(info.value)


SYNC_IN_CHILD = [
    "f_retry",
    "f_memoize",
    "f_circuit_breaker",
    "f_breaker_object",
    "f_timeout",
    "f_rate_limit",
    "f_bulkhead",
    "f_batch_calls",
    "f_hedge",
    "f_periodic",
    "f_policy",
    "f_count_calls",
    "f_stacked",
    "f_enforce_types",
]
ASYNC_IN_CHILD = ["af_retry", "af_memoize", "af_periodic", "af_batch_calls"]


def test_spawn_process_pool_runs_decorated_functions() -> None:
    tests_dir = os.path.dirname(os.path.abspath(h.__file__))
    ctx = multiprocessing.get_context("spawn")
    before = h.f_count_calls.stats.calls
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=1,  # one worker: the per-process counter check below is exact
        mp_context=ctx,
        # Spawn re-imports _pickle_helpers by name; make sure tests/ is importable there.
        initializer=functools.partial(site.addsitedir, tests_dir),
    ) as pool:
        sync = {name: pool.submit(h.call_in_child, getattr(h, name), 1) for name in SYNC_IN_CHILD}
        aio = {name: pool.submit(h.run_async, getattr(h, name), 1) for name in ASYNC_IN_CHILD}
        direct = list(pool.map(h.f_retry, [1, 2, 3]))  # submitting the function itself
        seen = pool.submit(h.count_calls_seen).result(timeout=30)
        results = {name: f.result(timeout=30) for name, f in {**sync, **aio}.items()}
    assert results == dict.fromkeys(SYNC_IN_CHILD + ASYNC_IN_CHILD, 2)
    assert direct == [2, 3, 4]
    # f_count_calls ran once in the worker (directly and inside nothing else); the
    # parent's counter is untouched: state is per process.
    assert seen == 1
    assert h.f_count_calls.stats.calls == before
