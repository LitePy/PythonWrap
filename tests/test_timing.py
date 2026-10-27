from __future__ import annotations

import asyncio
import cProfile
import importlib
import inspect
import logging
import pstats
import tracemalloc

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.timing import (
    BenchmarkResult,
    MemoryRecord,
    TimingRecord,
    _format_bytes,
    benchmark,
    benchmark_async,
    measure_memory,
    profile,
    timing,
)


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ----------------------------------------------------------------------------- timing


def test_timing_bare_and_readable_units(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @timing
    def work(delay: float) -> str:
        fake_clock.advance(delay)
        return "ok"

    assert work(0.01234) == "ok"
    assert work(0.0005) == "ok"
    assert work(2.5) == "ok"
    assert work(125.0) == "ok"
    msgs = messages(caplog)
    assert msgs[0].endswith("work took 12.34 ms")
    assert msgs[1].endswith("work took 500.00 us")
    assert msgs[2].endswith("work took 2.50 s")
    assert msgs[3].endswith("work took 2 min 05.0 s")


def test_timing_call_form_and_wraps(fake_clock: FakeClock) -> None:
    def f() -> int:
        """doc"""
        return 1

    wrapped = timing(f)
    assert wrapped() == 1
    assert wrapped.__name__ == "f"
    assert wrapped.__doc__ == "doc"


def test_timing_error_is_logged_and_reraised(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")
    records: list[TimingRecord] = []

    @timing(callback=records.append)
    def boom() -> None:
        fake_clock.advance(0.5)
        raise ValueError("x")

    with pytest.raises(ValueError):
        boom()
    assert "raised ValueError after 500.00 ms" in messages(caplog)[0]
    assert records == [TimingRecord(boom.__qualname__, 0.5, False)]


def test_timing_threshold_and_callback_always(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")
    records: list[TimingRecord] = []

    @timing(threshold=0.1, callback=records.append)
    def work(delay: float) -> None:
        fake_clock.advance(delay)

    work(0.05)
    assert caplog.records == []
    work(0.2)
    assert len(caplog.records) == 1
    assert [r.elapsed for r in records] == [pytest.approx(0.05), pytest.approx(0.2)]
    assert all(r.success for r in records)


def test_timing_logger_name_and_level(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom.name")

    @timing(logger="custom.name", level=logging.DEBUG)
    def f() -> None: ...

    f()
    assert caplog.records[0].name == "custom.name"
    assert caplog.records[0].levelno == logging.DEBUG


def test_timing_callback_failure_is_swallowed(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    def bad(record: TimingRecord) -> None:
        raise RuntimeError("metrics down")

    @timing(callback=bad)
    def f() -> int:
        return 3

    assert f() == 3
    assert any("callback for" in m for m in messages(caplog))


def test_timing_async_measures_awaited_time(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @timing
    async def work() -> int:
        await _core.async_sleep(0.25)
        return 7

    assert inspect.iscoroutinefunction(work)
    assert asyncio.run(work()) == 7
    assert messages(caplog)[0].endswith("work took 250.00 ms")


def test_timing_async_error(fake_clock: FakeClock) -> None:
    records: list[TimingRecord] = []

    @timing(callback=records.append)
    async def work() -> None:
        await _core.async_sleep(0.1)
        raise KeyError("k")

    with pytest.raises(KeyError):
        asyncio.run(work())
    assert records[0].success is False
    assert records[0].elapsed == pytest.approx(0.1)


def test_timing_methods(fake_clock: FakeClock) -> None:
    records: list[TimingRecord] = []

    class C:
        @timing(callback=records.append)
        def inst(self) -> int:
            return 1

        @classmethod
        @timing(callback=records.append)
        def cls(cls) -> int:
            return 2

        @staticmethod
        @timing(callback=records.append)
        def stat() -> int:
            return 3

    assert (C().inst(), C.cls(), C.stat()) == (1, 2, 3)
    assert len(records) == 3


# --------------------------------------------------------------------------- benchmark


def test_benchmark_stats_and_str(fake_clock: FakeClock) -> None:
    delays = iter([9.0, 1.0, 2.0, 3.0])  # first is warmup
    calls: list[tuple[int, int]] = []

    def work(a: int, b: int = 0) -> None:
        calls.append((a, b))
        fake_clock.advance(next(delays))

    result = benchmark(work, args=(1,), kwargs={"b": 2}, runs=3, warmup=1)
    assert isinstance(result, BenchmarkResult)
    assert len(calls) == 4
    assert calls[0] == (1, 2)
    assert result.timings == (1.0, 2.0, 3.0)
    assert (result.runs, result.mean, result.median) == (3, 2.0, 2.0)
    assert (result.min, result.max) == (1.0, 3.0)
    assert result.stdev == pytest.approx(1.0)
    text = str(result)
    assert "work" in text
    assert "3 runs" in text
    assert "mean 2.00 s" in text


def test_benchmark_single_run_has_zero_stdev(fake_clock: FakeClock) -> None:
    result = benchmark(lambda: fake_clock.advance(0.1), runs=1, warmup=0)
    assert result.stdev == 0.0


def test_benchmark_validation_and_async_rejected() -> None:
    async def coro() -> None: ...

    with pytest.raises(TypeError, match="benchmark_async"):
        benchmark(coro)
    with pytest.raises(ValueError):
        benchmark(len, args=([],), runs=0)
    with pytest.raises(ValueError):
        benchmark(len, args=([],), warmup=-1)


def test_benchmark_async(fake_clock: FakeClock) -> None:
    async def work(delay: float) -> None:
        await _core.async_sleep(delay)

    result = asyncio.run(benchmark_async(work, args=(0.5,), runs=2, warmup=1))
    assert result.timings == (0.5, 0.5)
    assert result.name.endswith("work")
    assert len(fake_clock.sleeps) == 3


async def _noop_async() -> None: ...


def test_benchmark_async_rejects_sync_and_bad_runs() -> None:
    with pytest.raises(TypeError, match="benchmark"):
        asyncio.run(benchmark_async(len, args=([],)))
    with pytest.raises(ValueError):
        asyncio.run(benchmark_async(_noop_async, runs=0))


# ----------------------------------------------------------------------- measure_memory


def test_measure_memory_peak_and_net(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")
    records: list[MemoryRecord] = []
    assert not tracemalloc.is_tracing()

    @measure_memory(callback=records.append)
    def alloc() -> int:
        data = bytearray(2_000_000)
        n = len(data)
        del data
        return n

    assert alloc() == 2_000_000
    assert not tracemalloc.is_tracing()  # we started it, so we stopped it
    rec = records[0]
    assert rec.peak_bytes >= 2_000_000
    assert rec.net_bytes < 100_000
    assert "MiB" in messages(caplog)[0]


def test_measure_memory_keeps_existing_tracing() -> None:
    tracemalloc.start()
    try:

        @measure_memory
        def f() -> list[int]:
            return [0] * 1000

        f()
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_measure_memory_net_positive_and_error() -> None:
    records: list[MemoryRecord] = []
    keep: list[bytearray] = []

    @measure_memory(callback=records.append)
    def hold() -> None:
        keep.append(bytearray(500_000))

    hold()
    assert records[0].net_bytes >= 500_000

    @measure_memory(callback=records.append)
    def boom() -> None:
        raise RuntimeError

    with pytest.raises(RuntimeError):
        boom()
    assert len(records) == 2
    assert not tracemalloc.is_tracing()


def test_measure_memory_async() -> None:
    records: list[MemoryRecord] = []

    @measure_memory(callback=records.append)
    async def work() -> int:
        data = bytearray(1_000_000)
        await asyncio.sleep(0)
        return len(data)

    assert inspect.iscoroutinefunction(work)
    assert asyncio.run(work()) == 1_000_000
    assert records[0].peak_bytes >= 1_000_000


def test_measure_memory_nested_stops_once() -> None:
    @measure_memory
    def inner() -> None:
        bytearray(1000)

    @measure_memory
    def outer() -> None:
        inner()
        assert tracemalloc.is_tracing()

    outer()
    assert not tracemalloc.is_tracing()


def test_format_bytes_units() -> None:
    assert _format_bytes(512) == "512 B"
    assert _format_bytes(2048) == "2.00 KiB"
    assert _format_bytes(-2048, signed=True) == "-2.00 KiB"
    assert _format_bytes(5 * 1024**3) == "5.00 GiB"
    assert _format_bytes(1024**4) == "1024.00 GiB"


# --------------------------------------------------------------------------- profile


def helper_work() -> int:
    return sum(i * i for i in range(2000))


def test_profile_logs_top_rows(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")
    seen: list[pstats.Stats] = []

    @profile(limit=5, callback=seen.append)
    def run() -> int:
        return helper_work()

    assert run() == helper_work()
    msg = messages(caplog)[0]
    assert "Profile of" in msg
    assert "helper_work" in msg
    assert isinstance(seen[0], pstats.Stats)


def test_profile_bare_no_limit_and_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @profile(limit=None, sort="tottime")
    def boom() -> None:
        raise ValueError

    with pytest.raises(ValueError):
        boom()
    assert "Profile of" in messages(caplog)[0]

    @profile
    def ok() -> int:
        return 1

    assert ok() == 1


def test_profile_invalid_sort() -> None:
    with pytest.raises(ValueError):
        profile(sort="nonsense")


def test_profile_nested_warns_once_and_runs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @profile
    def inner() -> int:
        return 5

    outer_profiler = cProfile.Profile()
    outer_profiler.enable()
    try:
        results = [inner(), inner()]
    finally:
        outer_profiler.disable()
    assert results == [5, 5]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_profile_async(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @profile
    async def work() -> int:
        await asyncio.sleep(0)
        return helper_work()

    assert inspect.iscoroutinefunction(work)
    assert asyncio.run(work()) == helper_work()
    assert "Profile of" in messages(caplog)[0]


def test_timing_skips_formatting_when_level_disabled(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    timing_mod = importlib.import_module("python_wrap.timing")

    caplog.set_level(logging.ERROR, logger="python_wrap")
    seen: list[float] = []

    def spy(seconds: float) -> str:
        seen.append(seconds)
        return "x"

    monkeypatch.setattr(timing_mod, "_format_duration", spy)
    records: list[object] = []

    @timing_mod.timing(callback=records.append)
    def ok() -> int:
        return 1

    @timing_mod.timing(callback=records.append)
    def bad() -> int:
        raise ValueError

    ok()
    with pytest.raises(ValueError):
        bad()
    assert seen == []  # nothing formatted, yet the callback ran for both calls
    assert len(records) == 2
    assert caplog.records == []
