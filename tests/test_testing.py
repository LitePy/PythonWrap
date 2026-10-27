from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.testing import (
    FakeClock,
    disable_caching,
    disable_rate_limits,
    disable_retries,
    eventually,
    eventually_sync,
    fake_clock,
    fast_mode,
    overrides,
    spy,
    spy_on,
)


@pytest.fixture(autouse=True)
def _clean_overrides() -> None:
    assert _core.overrides == _core.Overrides()


# ------------------------------------------------------------------------ overrides


def test_defaults() -> None:
    o = _core.overrides
    assert (o.no_sleep, o.max_attempts, o.disable_rate_limits, o.disable_caching) == (
        False,
        None,
        False,
        False,
    )


def test_each_helper_sets_and_restores() -> None:
    with fast_mode():
        assert _core.overrides.no_sleep
    with disable_retries():
        assert _core.overrides.max_attempts == 1
    with disable_rate_limits():
        assert _core.overrides.disable_rate_limits
    with disable_caching():
        assert _core.overrides.disable_caching
    assert _core.overrides == _core.Overrides()


def test_restores_on_exception() -> None:
    with pytest.raises(RuntimeError), fast_mode():
        raise RuntimeError
    assert not _core.overrides.no_sleep


def test_nesting_restores_previous_values() -> None:
    with overrides(max_attempts=5, no_sleep=True):
        with overrides(max_attempts=2):
            assert _core.overrides.max_attempts == 2
            assert _core.overrides.no_sleep
            with disable_retries():
                assert _core.overrides.max_attempts == 1
            assert _core.overrides.max_attempts == 2
        assert _core.overrides.max_attempts == 5
        with overrides(max_attempts=None):
            assert _core.overrides.max_attempts is None
        assert _core.overrides.max_attempts == 5
    assert _core.overrides == _core.Overrides()


def test_same_scope_object_is_reentrant() -> None:
    scope = fast_mode()
    with scope:
        with scope:
            assert _core.overrides.no_sleep
        assert _core.overrides.no_sleep
    assert not _core.overrides.no_sleep


def test_decorator_sync_and_async() -> None:
    @fast_mode()
    def sync_test(x: int) -> bool:
        """doc"""
        return _core.overrides.no_sleep and x == 1

    @disable_retries()
    async def async_test() -> int | None:
        await asyncio.sleep(0)
        return _core.overrides.max_attempts

    assert sync_test(1)
    assert sync_test.__doc__ == "doc"
    assert inspect.iscoroutinefunction(async_test)
    assert asyncio.run(async_test()) == 1
    assert _core.overrides == _core.Overrides()


def test_invalid_max_attempts() -> None:
    with pytest.raises(ValueError):
        overrides(max_attempts=0)
    with pytest.raises(ValueError):
        overrides(max_attempts="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _core.set_overrides(bogus=1)
    with pytest.raises(ValueError):
        _core.set_overrides(max_attempts=-1)


def test_overrides_visible_from_other_threads() -> None:
    seen: list[bool] = []
    with fast_mode():
        t = threading.Thread(target=lambda: seen.append(_core.overrides.no_sleep))
        t.start()
        t.join()
    assert seen == [True]


def test_threads_toggling_disjoint_fields_restore() -> None:
    both_in = threading.Barrier(2, timeout=5)
    seen: list[tuple[bool, int | None]] = []

    def worker(scope_factory: Callable[[], Any]) -> None:
        with scope_factory():
            both_in.wait()
            seen.append((_core.overrides.no_sleep, _core.overrides.max_attempts))
            both_in.wait()

    threads = [
        threading.Thread(target=worker, args=(fast_mode,)),
        threading.Thread(target=worker, args=(disable_retries,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [(True, 1), (True, 1)]
    assert _core.overrides == _core.Overrides()


def test_no_sleep_makes_core_sleep_instant() -> None:
    import time

    start = time.perf_counter()
    with fast_mode():
        _core.sleep(5)
        asyncio.run(_core.async_sleep(5))
    assert time.perf_counter() - start < 1


async def test_no_sleep_async_still_yields() -> None:
    order: list[str] = []

    async def other() -> None:
        order.append("other")

    task = asyncio.create_task(other())
    with fast_mode():
        await _core.async_sleep(10)
    order.append("after")
    await task
    assert order == ["other", "after"]


# --------------------------------------------------------------------------- clock


def test_fakeclock_basics() -> None:
    clock = FakeClock(start=5.0)
    assert clock.now == 5.0 and clock.time() == 5.0
    clock.advance(2)
    clock.sleep(3)
    clock.sleep(-1)
    assert clock.now == 10.0
    assert clock.sleeps == [3, -1]
    assert clock.total_slept == 2
    before = time.monotonic()
    assert before <= FakeClock().now <= time.monotonic()
    assert FakeClock(start=1000.0).now == 1000.0
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_fake_clock_patches_and_restores() -> None:
    before = (_core.monotonic, _core.perf_counter, _core.sleep, _core.async_sleep)
    with fake_clock() as clock:
        t0 = clock.now
        assert _core.monotonic() == t0
        _core.sleep(2)
        assert _core.perf_counter() == t0 + 2
        assert clock.sleeps == [2]
    assert (_core.monotonic, _core.perf_counter, _core.sleep, _core.async_sleep) == before


def test_fake_clock_nesting_and_custom_clock() -> None:
    outer_clock = FakeClock(start=1)
    with fake_clock(outer_clock):
        with fake_clock() as inner:
            assert _core.monotonic() == inner.now
            assert inner is not outer_clock
        assert _core.monotonic() == 1


async def test_fake_clock_async_sleep_yields_to_other_tasks() -> None:
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    with fake_clock() as clock:
        task = asyncio.create_task(other())
        await _core.async_sleep(3)
        assert ran == ["other"]
        await task
    assert clock.now == clock.start + 3


def test_fake_clock_fixture_is_public_clock(fake_clock: FakeClock) -> None:
    assert isinstance(fake_clock, FakeClock)
    _core.sleep(1)
    assert fake_clock.sleeps == [1]


def test_fakeclock_thread_safe_advance() -> None:
    clock = FakeClock(start=0)

    def work() -> None:
        for _ in range(1000):
            clock.advance(1)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert clock.now == 4000


# ---------------------------------------------------------------------------- spy


def test_spy_records_sync_calls() -> None:
    @spy
    def add(a: int, b: int = 0) -> int:
        """Add."""
        return a + b

    assert not add.called
    assert add(1, b=2) == 3
    assert add(4) == 4
    assert add.call_count == 2 and add.called
    assert add.__name__ == "add" and add.__doc__ == "Add."
    first = add.calls[0]
    assert (first.args, first.kwargs, first.result, first.exception) == ((1,), {"b": 2}, 3, None)
    add.assert_called_with(4)
    with pytest.raises(AssertionError, match="expected last call"):
        add.assert_called_with(9)
    add.reset()
    assert add.call_count == 0
    with pytest.raises(AssertionError, match="never called"):
        add.assert_called_with()


def test_spy_records_exceptions_and_duration(fake_clock: FakeClock) -> None:
    def boom() -> None:
        fake_clock.advance(2)
        raise KeyError("x")

    s = spy(boom)
    with pytest.raises(KeyError):
        s()
    call = s.calls[0]
    assert isinstance(call.exception, KeyError)
    assert call.result is None
    assert call.duration == 2


async def test_spy_async() -> None:
    async def double(x: int) -> int:
        return x * 2

    async def fail() -> None:
        raise ValueError

    s = spy(double)
    assert _core.is_async(s)
    assert await s(4) == 8
    s.assert_called_with(4)
    assert s.calls[0].result == 8

    f = spy(fail)
    with pytest.raises(ValueError):
        await f()
    assert isinstance(f.calls[0].exception, ValueError)


def test_spy_as_method_decorator_records_self() -> None:
    class Svc:
        @spy
        def go(self, x: int) -> int:
            return x + 1

    svc = Svc()
    assert svc.go(1) == 2
    assert Svc.go.call_count == 1
    assert Svc.go.calls[0].args == (svc, 1)


def test_spy_on_module_restores() -> None:
    import math

    original = math.floor
    with spy_on(math, "floor") as s:
        assert math.floor(2.5) == 2
        assert s.call_count == 1
    assert math.floor is original


def test_spy_on_class_instance_static_and_class_methods() -> None:
    class Thing:
        def inst(self, x: int) -> int:
            return x

        @staticmethod
        def stat(x: int) -> int:
            return x * 2

        @classmethod
        def cls(cls, x: int) -> int:
            return x * 3

    t = Thing()
    with spy_on(Thing, "inst") as s_inst:
        assert t.inst(1) == 1
        assert s_inst.calls[0].args == (t, 1)
    with spy_on(Thing, "stat") as s_stat:
        assert Thing.stat(2) == 4 and t.stat(2) == 4
        assert s_stat.call_count == 2
    with spy_on(Thing, "cls") as s_cls:
        assert Thing.cls(2) == 6
        assert s_cls.calls[0].args == (2,)
    assert isinstance(Thing.__dict__["stat"], staticmethod)
    assert isinstance(Thing.__dict__["cls"], classmethod)
    assert Thing.__dict__["inst"] is not None and not hasattr(Thing.__dict__["inst"], "calls")

    with spy_on(t, "inst") as s_obj:
        t.inst(7)
        assert s_obj.calls[0].args == (7,)
    assert "inst" not in t.__dict__


def test_spy_on_inherited_attribute_is_deleted_on_exit() -> None:
    class Base:
        def f(self) -> int:
            return 1

    class Child(Base):
        pass

    with spy_on(Child, "f") as s:
        assert Child().f() == 1
        assert s.called
    assert "f" not in Child.__dict__
    assert Child().f() == 1


def test_spy_on_missing_attribute() -> None:
    with pytest.raises(AttributeError), spy_on(object(), "nope"):
        pass


def test_spy_thread_safe() -> None:
    s = spy(lambda: None)

    def work() -> None:
        for _ in range(500):
            s()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.call_count == 2000


# ---------------------------------------------------------------------- eventually


async def test_eventually_passes_after_a_while() -> None:
    state = {"n": 0}

    def check() -> None:
        state["n"] += 1
        assert state["n"] >= 3

    await eventually(check, timeout=1, interval=0.001)
    assert state["n"] == 3


async def test_eventually_async_assertion() -> None:
    state = {"n": 0}

    async def check() -> None:
        await asyncio.sleep(0)
        state["n"] += 1
        assert state["n"] >= 2

    await eventually(check, interval=0.001)


async def test_eventually_timeout_reports_wait_and_last_error() -> None:
    def check() -> None:
        assert 1 == 2, "never true"

    with fake_clock() as clock, pytest.raises(AssertionError) as info:
        await eventually(check, timeout=1.0, interval=0.25)
    msg = str(info.value)
    assert "never true" in msg
    assert "still failing after 1.000s and 5 attempt(s)" in msg
    assert isinstance(info.value.__cause__, AssertionError)
    assert clock.sleeps == [0.25] * 4


async def test_eventually_propagates_other_exceptions() -> None:
    def check() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        await eventually(check)


async def test_eventually_always_tries_once() -> None:
    calls: list[int] = []
    await eventually(lambda: calls.append(1), timeout=0)
    assert calls == [1]


def test_eventually_sync_with_background_thread() -> None:
    done = threading.Event()
    threading.Timer(0.01, done.set).start()

    def check() -> None:
        assert done.is_set()

    eventually_sync(check, timeout=2, interval=0.001)


def test_eventually_sync_timeout(fake_clock: FakeClock) -> None:
    def check() -> None:
        raise AssertionError("no")

    with pytest.raises(AssertionError, match=r"still failing after 0\.500s and 3 attempt"):
        eventually_sync(check, timeout=0.5, interval=0.25)


# ------------------------------------------------------------------- pytest plugin


def test_module_imports_without_pytest() -> None:
    code = (
        "import sys; sys.modules['pytest'] = None;"
        "import python_wrap.testing as t;"
        "assert not hasattr(t, 'pw_fake_clock');"
        "assert t.fast_mode"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_plugin_fixtures(tmp_path: Path) -> None:
    (tmp_path / "test_plugin_demo.py").write_text(
        textwrap.dedent(
            """
            from python_wrap import _core

            def test_clock(pw_fake_clock):
                _core.sleep(3)
                assert pw_fake_clock.sleeps == [3]
                assert _core.monotonic() == pw_fake_clock.now

            def test_fast(pw_fast_mode):
                assert _core.overrides.no_sleep

            def test_after():
                assert not _core.overrides.no_sleep
            """
        )
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "python_wrap.testing",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:asyncio",
            "-q",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 passed" in proc.stdout


def test_eventually_sync_under_fast_mode_pauses_real_time():
    import time as _time

    from python_wrap.testing import eventually_sync, fast_mode

    calls = []

    def never() -> None:
        calls.append(1)
        raise AssertionError("not yet")

    with fast_mode():
        started = _time.monotonic()
        with pytest.raises(AssertionError):
            eventually_sync(never, timeout=0.05, interval=0.01)
        assert _time.monotonic() - started >= 0.05
    # A busy loop would make thousands of attempts in 50ms.
    assert len(calls) < 20
