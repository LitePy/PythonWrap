"""Regression coverage for the report fixes and local 0.3 primitives."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from python_wrap import (
    BatchResult,
    Bulkhead,
    ContextFilter,
    EnvSource,
    FeatureDisabled,
    HealthRegistry,
    LocalLease,
    MemoryCheckpoint,
    PolicyRegistry,
    Quota,
    QuotaExhausted,
    aprocess_batches,
    bind,
    expiring,
    idempotent,
    in_cause_chain,
    message_contains,
    process_batches,
    retry,
    single_instance,
    sqlstate,
    switch,
    timed,
    unbind,
)
from python_wrap.logs import current_log_context
from python_wrap.testing import assert_cache_hit, assert_retried, disabled, fake_clock


def test_context_filter_preserves_explicit_values_and_reserved_attributes():
    record = logging.LogRecord("app", logging.INFO, "x", 1, "hello", (), None)
    record.user = "explicit"
    token = bind(user="context", request_id="r1", name="cannot replace", message="cannot replace")
    try:
        assert ContextFilter({"user": "-", "job": "none"}).filter(record)
        assert record.user == "explicit" and record.request_id == "r1"
        assert record.name == "app" and not hasattr(record, "message")
        assert record.context["user"] == "context"
    finally:
        unbind(token)
    assert current_log_context() == {}


@pytest.mark.asyncio
async def test_bind_isolated_between_tasks():
    async def run(value):
        token = bind(user=value)
        try:
            await asyncio.sleep(0)
            return current_log_context()["user"]
        finally:
            unbind(token)

    assert await asyncio.gather(run("a"), run("b")) == ["a", "b"]
    assert current_log_context() == {}


def test_expiring_refresh_margin_invalidation_and_failure():
    with fake_clock() as clock:
        calls = []

        @expiring(refresh_margin=2)
        def token():
            calls.append(1)
            return str(len(calls)), 10

        assert token() == token() == "1"
        clock.advance(8)
        assert token() == "2"
        token.invalidate()
        assert token() == "3"
        assert token.cache_info().currsize == 1


@pytest.mark.asyncio
async def test_async_expiring_coalesces_refresh():
    count = 0

    @expiring(refresh_margin=0)
    async def token():
        nonlocal count
        count += 1
        await asyncio.sleep(0)
        return "secret", 60

    assert await asyncio.gather(*(token() for _ in range(20))) == ["secret"] * 20
    assert count == 1
    token.invalidate()
    assert await token() == "secret" and count == 2


def test_predicates_follow_driver_orig_and_cycles():
    leaf = Exception("08S01", "connection dropped")
    outer = RuntimeError("wrapper")
    outer.orig = leaf
    leaf.__cause__ = outer
    predicate = in_cause_chain(sqlstate("08") | message_contains("deadlock"))
    assert predicate(outer)
    assert (predicate & ~message_contains("fatal"))(outer)
    assert not predicate(ValueError("bad input"))


def test_local_quota_is_atomic():
    quota = Quota(10)

    def take(_):
        try:
            quota.take()
            return 1
        except QuotaExhausted:
            return 0

    with ThreadPoolExecutor(20) as pool:
        assert sum(pool.map(take, range(100))) == 10
    assert quota.remaining == 0
    quota.reset()
    assert quota.remaining == 10


def test_local_lease_expiry_ownership_and_renewal():
    backend = LocalLease()
    with fake_clock() as clock:
        assert backend.acquire("x", "a", 10)
        assert not backend.acquire("x", "b", 10)
        clock.advance(11)
        assert backend.acquire("x", "b", 10)
        assert not backend.release("x", "a")
        assert backend.renew("x", "b", 20)
        clock.advance(11)
        assert not backend.acquire("x", "c", 10)
        assert backend.release("x", "b")


def test_single_instance_releases_on_original_exception():
    backend = LocalLease()

    @single_instance(key="x", backend=backend, renew=False)
    def work():
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        work()
    assert backend.acquire("x", "next", 1)


def test_idempotency_none_failures_and_expiry():
    with fake_clock() as clock:
        calls = []

        @idempotent(key=lambda x: str(x), ttl=10)
        def work(x):
            calls.append(x)
            if x == "error":
                raise ValueError("bad")
            return None

        assert work("ok") is None and work("ok") is None
        assert calls == ["ok"]
        clock.advance(11)
        work("ok")
        assert calls == ["ok", "ok"]
        for _ in range(2):
            with pytest.raises(ValueError):
                work("error")
        assert calls.count("error") == 2


def test_idempotency_concurrent_waiters_execute_once():
    started, release = threading.Event(), threading.Event()
    calls = []

    @idempotent(key="only", in_progress="wait")
    def work():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return 42

    with ThreadPoolExecutor(4) as pool:
        leader = pool.submit(work)
        assert started.wait(2)
        others = [pool.submit(work) for _ in range(3)]
        release.set()
        assert [leader.result(), *(future.result() for future in others)] == [42] * 4
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_idempotency_and_shared_bulkhead_release():
    backend = LocalLease()
    calls = []

    @idempotent(key="async", in_progress="wait")
    async def work():
        calls.append(1)
        await asyncio.sleep(0.01)
        return 7

    assert await asyncio.gather(work(), work()) == [7, 7]
    assert len(calls) == 1
    bulkhead = Bulkhead(1, backend=backend, name="shared", timeout=0)
    async with bulkhead:
        assert bulkhead.active == 1
        with pytest.raises(Exception) as error:
            async with bulkhead:
                pass
        assert type(error.value).__name__ == "BulkheadFull"
    assert bulkhead.active == 0
    async with bulkhead:
        pass


def test_batch_outcomes_checkpoint_and_resume():
    point = MemoryCheckpoint()
    progress = []

    def handler(item):
        if item == 2:
            raise ValueError("invalid")
        return item * 2

    result = process_batches(
        [1, 2, 3],
        handler,
        chunk_size=2,
        checkpoint=point,
        on_progress=lambda n, index: progress.append((n, index)),
    )
    assert isinstance(result, BatchResult)
    assert [o.result for o in result.succeeded] == [2, 6]
    assert result.to_rows() == [{"item": 2, "status": "failed", "reason": "invalid"}]
    assert point.load() == 3 and progress == [(2, 2), (3, 3)]
    assert not process_batches([1, 2, 3], handler, checkpoint=point).outcomes
    assert process_batches([1, 2, 3, 4], handler, checkpoint=point).succeeded[0].result == 8


@pytest.mark.asyncio
async def test_async_batch_checkpoint_cancellation():
    point = MemoryCheckpoint()

    async def handler(item):
        if item == 2:
            raise asyncio.CancelledError
        return item

    with pytest.raises(asyncio.CancelledError):
        await aprocess_batches([1, 2, 3], handler, chunk_size=1, checkpoint=point)
    assert point.load() == 1


def test_switch_reads_dynamic_env_and_cache(monkeypatch):
    @switch("import", source=EnvSource(), when_off="skip")
    def work():
        return 1

    monkeypatch.setenv("PW_SWITCH_IMPORT", "false")
    assert work() is None
    monkeypatch.setenv("PW_SWITCH_IMPORT", "true")
    assert work() == 1

    @switch("import", source=EnvSource())
    def strict():
        return 1

    monkeypatch.setenv("PW_SWITCH_IMPORT", "false")
    with pytest.raises(FeatureDisabled):
        strict()


def test_named_registry_keeps_state_and_reloads_atomically():
    registry = PolicyRegistry()
    registry.register("api", retry={"max_attempts": 2, "delay": 0})
    calls = []

    @registry.resilient("api")
    def work():
        calls.append(1)
        raise ValueError("failed")

    with pytest.raises(ValueError):
        work()
    assert len(calls) == 2
    registry.register("api", retry={"max_attempts": 1, "delay": 0})
    with pytest.raises(ValueError):
        work()
    assert len(calls) == 3
    assert registry.describe("api")["api"]["order"] == ("retry",)
    with pytest.raises(ValueError):
        registry.load_from_mapping({"good": {"retry": {"delay": 0}}, "bad": {"typo": 1}})
    assert "good" not in registry.describe()


def test_timed_masks_nested_values_and_keeps_original_error(caplog):
    caplog.set_level(logging.WARNING, logger="python_wrap")
    with fake_clock() as clock:

        @timed(warn_over="2s", log_args=True)
        def work(author, payload):
            clock.advance(3)
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            work("author", {"password": "sensitive"})
    assert "sensitive" not in caplog.text and "author" in caplog.text


def test_testing_assertions_and_disabled():
    from python_wrap import memoize

    @retry(max_attempts=2, delay=0)
    def bad():
        raise ValueError()

    with pytest.raises(ValueError):
        bad()
    assert_retried(bad, times=1)

    @memoize
    def cached():
        return 1

    cached()
    cached()
    assert_cache_hit(cached)
    with disabled():
        cached()
        with pytest.raises(ValueError):
            bad()
    assert_cache_hit(cached)
    assert_retried(bad, times=1)


def test_health_does_not_call_registered_user_functions_or_leak_errors():
    registry = HealthRegistry()
    calls = []

    def work():
        calls.append(1)

    registry.register("func", work)
    registry.register_check("bad", lambda: 1 / 0)
    report = registry.report()
    assert not calls
    assert report["func"]["checked"] is False
    assert report["bad"] == {"healthy": False, "error": "ZeroDivisionError"}


@pytest.mark.asyncio
async def test_async_switch_preserves_awaitable_return_value():
    @switch("on", source=lambda _: True)
    async def work():
        return asyncio.sleep(0, result=7)

    value = await work()
    assert asyncio.iscoroutine(value)
    assert await value == 7


def test_sync_switch_rejects_async_fallback():
    async def fallback():
        return 1

    with pytest.raises(TypeError):

        @switch("off", source=lambda _: False, when_off=fallback)
        def work():
            return 1


def test_failure_window_does_not_count_old_failures():
    from python_wrap import CircuitBreaker, CircuitOpenError

    with fake_clock() as clock:
        breaker = CircuitBreaker(failure_threshold=2, window=10)

        def fail():
            raise ConnectionError()

        with pytest.raises(ConnectionError):
            breaker.call(fail)
        clock.advance(11)
        with pytest.raises(ConnectionError):
            breaker.call(fail)
        assert breaker.state.value == "closed"
        with pytest.raises(ConnectionError):
            breaker.call(fail)
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: 1)


def test_retry_does_not_retry_open_circuit_and_none_results_receive_delay():
    from python_wrap import CircuitBreaker, CircuitOpenError

    with fake_clock() as clock:
        breaker = CircuitBreaker()
        breaker.force_open()

        @retry(max_attempts=4, delay=1)
        def call():
            return breaker.call(lambda: 1)

        with pytest.raises(CircuitOpenError):
            call()
        assert clock.sleeps == []
        calls = []

        @retry(
            max_attempts=2,
            retry_if_result=lambda result: result is None,
            delay_from=lambda outcome: calls.append(outcome) or 2,
        )
        def empty():
            return None

        assert empty() is None and calls == [None]
        assert clock.sleeps == [2]


def test_named_policy_shares_breaker_state_between_functions():
    from python_wrap import CircuitOpenError

    registry = PolicyRegistry()
    registry.register("dependency", breaker={"failure_threshold": 1})

    @registry.resilient("dependency")
    def first():
        raise ValueError("down")

    @registry.resilient("dependency")
    def second():
        return 7

    with pytest.raises(ValueError):
        first()
    with pytest.raises(CircuitOpenError):
        second()
    registry.describe()
    with pytest.raises(CircuitOpenError):
        second()
    registry.register("dependency", breaker={"failure_threshold": 1})
    assert second() == 7


@pytest.mark.asyncio
async def test_async_batch_settles_cancelled_checkpoint_before_returning():
    entered, release = threading.Event(), threading.Event()

    class Checkpoint:
        index = 0

        def load(self):
            return self.index

        def save(self, index):
            entered.set()
            assert release.wait(2)
            self.index = index

    point = Checkpoint()

    async def handler(value):
        return value

    task = asyncio.create_task(aprocess_batches([1], handler, chunk_size=1, checkpoint=point))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert point.index == 1
