"""SQLiteBreakerState: contract tests ported from the Redis backend, plus a multi-process test."""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from python_wrap import CircuitBreaker, CircuitOpenError, RedisBreakerState
from python_wrap.local_state import SQLiteBreakerState
from test_redis_backends import redis_client as redis_fixture

# Reuse the disposable-Redis fixture for the parity tests (skips without redis-server).
redis_client = redis_fixture


@pytest.fixture
def state(tmp_path):
    backend = SQLiteBreakerState(tmp_path / "breakers.db")
    yield backend
    backend.close()


def bad():
    raise ConnectionError("down")


def test_shared_breaker_rejects_other_instances_and_resets(state):
    a = CircuitBreaker(name="remote", backend=state, failure_threshold=2, recovery_timeout=0.04)
    b = CircuitBreaker(name="remote", backend=state, failure_threshold=2, recovery_timeout=0.04)
    with pytest.raises(ConnectionError):
        a.call(bad)
    with pytest.raises(ConnectionError):
        b.call(bad)
    with pytest.raises(CircuitOpenError):
        a.call(lambda: 1)
    assert b.state.value == "open"
    time.sleep(0.05)
    assert b.call(lambda: 7) == 7
    assert a.state.value == "closed"
    a.force_open()
    with pytest.raises(CircuitOpenError):
        b.call(lambda: 1)
    a.reset()
    assert b.call(lambda: 1) == 1


def test_names_are_independent(state):
    a = CircuitBreaker(name="one", backend=state, failure_threshold=1)
    b = CircuitBreaker(name="two", backend=state, failure_threshold=1)
    a.force_open()
    assert b.state.value == "closed"


def test_half_open_capacity_generation_and_reset_stats(state):
    breaker = CircuitBreaker(
        name="trials",
        backend=state,
        failure_threshold=1,
        recovery_timeout=0.03,
        half_open_max_calls=1,
    )
    breaker.force_open()
    time.sleep(0.04)
    old_generation, trial = breaker._acquire()
    assert trial
    with pytest.raises(CircuitOpenError):
        breaker._acquire()
    breaker.reset()  # fences out the old trial's late failure
    breaker._failure(old_generation, trial)
    assert breaker.state.value == "closed"
    assert breaker.stats.failures == 0 and breaker.stats.rejected == 0
    assert breaker.call(lambda: 7) == 7
    assert breaker.stats.successes == 1


def test_failure_window_ignores_old_failures(state):
    breaker = CircuitBreaker(
        name="windowed", backend=state, failure_threshold=2, recovery_timeout=1, window=0.3
    )
    with pytest.raises(ConnectionError):
        breaker.call(bad)
    time.sleep(0.4)
    with pytest.raises(ConnectionError):
        breaker.call(bad)
    assert breaker.state.value == "closed"
    with pytest.raises(ConnectionError):
        breaker.call(bad)
    assert breaker.state.value == "open"


def test_success_resets_consecutive_failures_and_totals(state):
    breaker = CircuitBreaker(name="totals", backend=state, failure_threshold=3)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            breaker.call(bad)
    assert breaker.failure_count == 2
    assert breaker.call(lambda: 1) == 1
    assert breaker.failure_count == 0
    stats = breaker.stats
    assert (stats.failures, stats.successes, stats.rejected) == (2, 1, 0)
    assert stats.opened_at is None


def test_rejection_reports_retry_after_and_counts(state):
    breaker = CircuitBreaker(name="rej", backend=state, failure_threshold=1, recovery_timeout=30)
    with pytest.raises(ConnectionError):
        breaker.call(bad)
    with pytest.raises(CircuitOpenError) as info:
        breaker.call(lambda: 1)
    assert 29 < info.value.retry_after <= 30
    assert breaker.stats.rejected == 1 and breaker.stats.opened_at is not None


def test_trial_deadline_frees_a_crashed_probe_slot(state, fake_clock):
    # fake_clock patches _core.wall_time, which the backend reads.
    breaker = CircuitBreaker(
        name="probe", backend=state, failure_threshold=1, recovery_timeout=5, half_open_max_calls=1
    )
    breaker.force_open()
    fake_clock.advance(6)
    assert breaker._acquire()[1]  # a probe takes the slot and "crashes"
    with pytest.raises(CircuitOpenError):
        breaker._acquire()
    fake_clock.advance(6)  # past the trial deadline: slot is reissued
    assert breaker._acquire()[1]


def test_state_changes_are_reported_to_listener(state):
    seen = []
    breaker = CircuitBreaker(
        name="listen",
        backend=state,
        failure_threshold=1,
        on_state_change=lambda b, old, new: seen.append((old.value, new.value)),
    )
    with pytest.raises(ConnectionError):
        breaker.call(bad)
    assert seen == [("closed", "open")]


def test_concurrent_failures_are_all_counted(state):
    breaker = CircuitBreaker(name="race", backend=state, failure_threshold=1000)

    def fail(_):
        with pytest.raises(ConnectionError):
            breaker.call(bad)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(fail, range(80)))
    assert breaker.stats.failures == 80 and breaker.failure_count == 80


def test_database_uses_wal(state):
    state.operate(SimpleNamespace(**_settings("wal")), "state")
    with sqlite3.connect(state.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_validation(tmp_path):
    for bad_path in ("", ":memory:"):
        with pytest.raises(ValueError):
            SQLiteBreakerState(bad_path)
    with pytest.raises(ValueError):
        SQLiteBreakerState(tmp_path / "x.db", busy_timeout=-1)


def test_error_propagates_not_healthy(tmp_path):
    backend = SQLiteBreakerState(tmp_path / "missing" / "dir" / "b.db")
    breaker = CircuitBreaker(name="x", backend=backend)
    with pytest.raises(sqlite3.OperationalError):
        breaker.call(lambda: 1)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_connection_reopened_after_fork(state):
    breaker = CircuitBreaker(name="forked", backend=state, failure_threshold=1)
    breaker.force_open()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            code = 0 if breaker.state.value == "open" else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert breaker.state.value == "open"  # parent connection still healthy


# -- multiprocessing ---------------------------------------------------------------------


def _fail_worker(path, ready):
    backend = SQLiteBreakerState(path)
    breaker = CircuitBreaker(
        name="vendor", backend=backend, failure_threshold=3, recovery_timeout=60
    )
    for _ in range(3):
        with contextlib.suppress(ConnectionError):
            breaker.call(bad)
    ready.set()


def _observer(path, ready, results):
    backend = SQLiteBreakerState(path)
    breaker = CircuitBreaker(
        name="vendor", backend=backend, failure_threshold=3, recovery_timeout=60
    )
    if not ready.wait(30):
        results.put("timeout")
        return
    try:
        breaker.call(lambda: "reached the vendor")
    except CircuitOpenError:
        results.put("open")
    else:
        results.put("closed")


def test_failures_in_one_process_open_the_breaker_in_another(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path, ready, results = str(tmp_path / "shared.db"), ctx.Event(), ctx.Queue()
    observer = ctx.Process(target=_observer, args=(path, ready, results))
    failer = ctx.Process(target=_fail_worker, args=(path, ready))
    observer.start()
    failer.start()
    try:
        assert results.get(timeout=60) == "open"
    finally:
        for proc in (failer, observer):
            proc.join(timeout=30)
            if proc.is_alive():
                proc.kill()
    assert failer.exitcode == 0 and observer.exitcode == 0


# -- parity with the Redis implementation -----------------------------------------------


def _settings(name, **kw):
    base = {
        "name": name,
        "failure_threshold": 3,
        "window": None,
        "recovery_timeout": 1000.0,
        "half_open_max_calls": 2,
        "success_threshold": 2,
    }
    return base | kw


@pytest.mark.parametrize("window", [None, 1000.0])
def test_matches_redis_state_machine(redis_client, state, window):
    redis = RedisBreakerState(redis_client, prefix=f"parity{window}:")
    ops = [
        ("state", 0, False),
        ("admit", 0, False),
        ("failure", 0, False),
        ("success", 0, False),
        ("failure", 0, False),
        ("failure", 0, False),
        ("failure", 0, False),  # opens
        ("admit", 0, False),  # rejected
        ("failure", 1, False),  # stale generation
        ("state", 0, False),
        ("force_open", 0, False),
        ("neutral", 0, False),
        ("reset", 0, False),
        ("admit", 0, False),
        ("success", 3, False),
        ("failure", 3, False),
        ("state", 0, False),
    ]
    for op, generation, trial in ops:
        breaker = SimpleNamespace(**_settings("parity", window=window))
        mine = state.operate(breaker, op, generation, trial)
        theirs = redis.operate(breaker, op, generation, trial)
        for data in (mine, theirs):
            data.pop("opened")  # clocks differ; checked via wait below
        assert abs(mine.pop("wait") - theirs.pop("wait")) < 2000
        assert mine == theirs, op


def test_matches_redis_half_open_cycle(redis_client, state):
    redis = RedisBreakerState(redis_client, prefix="parity-half:")
    breaker = SimpleNamespace(**_settings("half", recovery_timeout=0.05, window=None))
    for backend in (state, redis):
        backend.operate(breaker, "force_open")
    time.sleep(0.08)
    results = []
    for backend in (state, redis):
        out = []
        for op, trial in (("admit", False), ("admit", False), ("admit", False)):
            row = backend.operate(breaker, op, 0, trial)
            out.append((row["admitted"], row["state"], row["trial"], row["old"]))
        gen = backend.operate(breaker, "state")["generation"]
        out.append(backend.operate(breaker, "success", gen, True)["state"])
        out.append(backend.operate(breaker, "success", gen, True)["state"])
        results.append(out)
    assert results[0] == results[1]
