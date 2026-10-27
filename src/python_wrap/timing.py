"""Performance decorators: timing, benchmarking, memory measurement and profiling."""

from __future__ import annotations

import cProfile
import io
import logging
import pstats
import statistics
import sys
import threading
import tracemalloc
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, overload

from python_wrap import _core
from python_wrap._core import F, LoggerLike, P, R
from python_wrap.logs import _instrument

__all__ = [
    "BenchmarkResult",
    "MemoryRecord",
    "TimingRecord",
    "benchmark",
    "benchmark_async",
    "measure_memory",
    "profile",
    "timing",
]


# --------------------------------------------------------------------------- helpers


def _format_duration(seconds: float) -> str:
    """Pick readable units: ``850.00 us``, ``12.34 ms``, ``1.50 s``, ``2 min 03.0 s``."""
    if seconds < 1e-3:
        return f"{seconds * 1e6:.2f} us"
    if seconds < 1:
        return f"{seconds * 1e3:.2f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} min {rest:04.1f} s"


def _format_bytes(count: int, *, signed: bool = False) -> str:
    """Human-readable byte count (binary units)."""
    sign = "-" if count < 0 else ("+" if signed else "")
    value = float(abs(count))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            text = f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
            return sign + text
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def _safe_callback(
    lg: logging.Logger, callback: Callable[[Any], None] | None, record: Any, name: str
) -> None:
    """Call a user callback; a failing metrics hook must not break the call."""
    if callback is None:
        return
    try:
        callback(record)
    except Exception:
        lg.exception("callback for %s failed", name)


# ---------------------------------------------------------------------------- timing


@dataclass(frozen=True)
class TimingRecord:
    """Result of one timed call, passed to ``timing(callback=...)``.

    Attributes:
        name: Qualified name of the function.
        elapsed: Seconds the call took.
        success: ``False`` if the call raised.
    """

    name: str
    elapsed: float
    success: bool


@overload
def timing(func: F, /) -> F: ...
@overload
def timing(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    threshold: float | None = None,
    callback: Callable[[TimingRecord], None] | None = None,
) -> Callable[[F], F]: ...
def timing(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    threshold: float | None = None,
    callback: Callable[[TimingRecord], None] | None = None,
) -> Any:
    """Measure and log how long each call takes.

    Logs ``<name> took 12.34 ms``. If the call raises, logs
    ``<name> raised ValueError after 12.34 ms`` and re-raises. Async functions are timed
    across the whole awaited coroutine (v0.1 measured about 0 s for them).

    Args:
        func: The function, when used as a bare ``@timing``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level.
        threshold: If set, successful calls are only logged when slower than this many
            seconds. Failed calls are always logged.
        callback: Called with a `TimingRecord` for *every* call, regardless of
            ``threshold``; use it to feed a metrics system. Errors in the callback are
            logged and swallowed.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @timing(threshold=0.5, callback=lambda r: print(r.name, r.elapsed))
        ... def slow(): ...
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> float:
            return _core.perf_counter()

        def finish(began: float, ok: bool, value: Any) -> None:
            elapsed = _core.perf_counter() - began
            if not ok:
                lg.log(
                    level,
                    "%s raised %s after %s",
                    name,
                    type(value).__name__,
                    _format_duration(elapsed),
                )
            elif threshold is None or elapsed > threshold:
                lg.log(level, "%s took %s", name, _format_duration(elapsed))
            _safe_callback(lg, callback, TimingRecord(name, elapsed, ok), name)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)


# ------------------------------------------------------------------------- benchmark


@dataclass(frozen=True)
class BenchmarkResult:
    """Statistics from `benchmark` or `benchmark_async`; all times are in seconds.

    Warm-up calls are not included. ``str(result)`` gives a one-line summary.

    Attributes:
        name: Qualified name of the benchmarked function.
        runs: Number of measured calls.
        timings: Duration of each measured call, in call order.
        mean: Arithmetic mean of ``timings``.
        median: Median of ``timings``.
        min: Fastest call.
        max: Slowest call.
        stdev: Sample standard deviation of ``timings`` (``0.0`` when ``runs`` is 1).
    """

    name: str
    runs: int
    timings: tuple[float, ...]
    mean: float
    median: float
    min: float
    max: float
    stdev: float

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.runs} runs, "
            f"mean {_format_duration(self.mean)}, median {_format_duration(self.median)}, "
            f"min {_format_duration(self.min)}, max {_format_duration(self.max)}, "
            f"stdev {_format_duration(self.stdev)}"
        )


def _summarise(name: str, timings: list[float]) -> BenchmarkResult:
    return BenchmarkResult(
        name=name,
        runs=len(timings),
        timings=tuple(timings),
        mean=statistics.fmean(timings),
        median=statistics.median(timings),
        min=min(timings),
        max=max(timings),
        stdev=statistics.stdev(timings) if len(timings) > 1 else 0.0,
    )


def _check_bench_args(runs: int, warmup: int) -> None:
    if runs < 1:
        raise ValueError(f"runs must be >= 1, got {runs}")
    if warmup < 0:
        raise ValueError(f"warmup must be >= 0, got {warmup}")


def benchmark(
    func: Callable[..., Any],
    /,
    *,
    args: tuple[Any, ...] = (),
    kwargs: Mapping[str, Any] | None = None,
    runs: int = 5,
    warmup: int = 1,
) -> BenchmarkResult:
    """Run ``func`` repeatedly and return timing statistics.

    BREAKING vs v0.1: this is a plain function, not a decorator. (The v0.1 decorator
    silently ran the function three times on every call.)

    Args:
        func: The synchronous callable to measure.
        args: Positional arguments for each call.
        kwargs: Keyword arguments for each call.
        runs: Number of measured runs (>= 1).
        warmup: Unmeasured runs executed first (>= 0).

    Returns:
        A `BenchmarkResult`.

    Raises:
        TypeError: If ``func`` is an ``async def`` function; use `benchmark_async`.
        ValueError: If ``runs`` or ``warmup`` is out of range.

    Example:
        >>> result = benchmark(sorted, args=([3, 1, 2],), runs=10)
        >>> print(result)  # doctest: +SKIP
    """
    if _core.is_async(func):
        raise TypeError(
            f"{_core.qualname(func)} is a coroutine function; use benchmark_async() instead"
        )
    _check_bench_args(runs, warmup)
    kw = dict(kwargs or {})
    for _ in range(warmup):
        func(*args, **kw)
    timings: list[float] = []
    for _ in range(runs):
        began = _core.perf_counter()
        func(*args, **kw)
        timings.append(_core.perf_counter() - began)
    return _summarise(_core.qualname(func), timings)


async def benchmark_async(
    func: Callable[..., Any],
    /,
    *,
    args: tuple[Any, ...] = (),
    kwargs: Mapping[str, Any] | None = None,
    runs: int = 5,
    warmup: int = 1,
) -> BenchmarkResult:
    """Like `benchmark`, for ``async def`` functions (each run is awaited).

    Args:
        func: The coroutine function to measure.
        args: Positional arguments for each call.
        kwargs: Keyword arguments for each call.
        runs: Number of measured runs (>= 1).
        warmup: Unmeasured runs executed first (>= 0).

    Returns:
        A `BenchmarkResult`.

    Raises:
        TypeError: If ``func`` is not an ``async def`` function; use `benchmark`.
        ValueError: If ``runs`` or ``warmup`` is out of range.

    Example:
        >>> result = await benchmark_async(fetch, args=(url,), runs=3)  # doctest: +SKIP
    """
    if not _core.is_async(func):
        raise TypeError(f"{_core.qualname(func)} is not a coroutine function; use benchmark()")
    _check_bench_args(runs, warmup)
    kw = dict(kwargs or {})
    for _ in range(warmup):
        await func(*args, **kw)
    timings: list[float] = []
    for _ in range(runs):
        began = _core.perf_counter()
        await func(*args, **kw)
        timings.append(_core.perf_counter() - began)
    return _summarise(_core.qualname(func), timings)


# --------------------------------------------------------------------- measure_memory


@dataclass(frozen=True)
class MemoryRecord:
    """Memory used by one call, passed to ``measure_memory(callback=...)``.

    Attributes:
        name: Qualified name of the function.
        peak_bytes: Highest traced allocation during the call, relative to its start.
        net_bytes: Traced memory still held at the end minus at the start (can be negative).
    """

    name: str
    peak_bytes: int
    net_bytes: int


_trace_lock = threading.Lock()
_trace_users = 0
_trace_started_by_us = False


def _trace_acquire() -> None:
    global _trace_users, _trace_started_by_us
    with _trace_lock:
        if _trace_users == 0 and not tracemalloc.is_tracing():
            tracemalloc.start()
            _trace_started_by_us = True
        _trace_users += 1


def _trace_release() -> None:
    global _trace_users, _trace_started_by_us
    with _trace_lock:
        _trace_users -= 1
        if _trace_users == 0 and _trace_started_by_us:
            tracemalloc.stop()
            _trace_started_by_us = False


@overload
def measure_memory(func: F, /) -> F: ...
@overload
def measure_memory(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    callback: Callable[[MemoryRecord], None] | None = None,
) -> Callable[[F], F]: ...
def measure_memory(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    callback: Callable[[MemoryRecord], None] | None = None,
) -> Any:
    """Measure the Python memory a call allocates, using `tracemalloc`.

    Works on every OS (v0.1 used ``resource.ru_maxrss``, which is Unix-only and reports
    process-wide peak in inconsistent units). Tracing is started if it is not already
    running and stopped afterwards only if this decorator started it. Logs
    ``<name> peak memory 1.00 MiB (net +12.00 KiB)``.

    Note:
        ``tracemalloc`` is process-global: allocations made by other threads or asyncio
        tasks while the call runs are counted too, and the peak counter is reset at the
        start of every measured call. Only memory allocated by Python (not by C
        extensions that bypass its allocator) is seen, and tracing slows the program.

    Args:
        func: The function, when used as a bare ``@measure_memory``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level.
        callback: Called with a `MemoryRecord` for every call.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @measure_memory
        ... def build():
        ...     return [0] * 1_000_000
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
            _trace_acquire()
            tracemalloc.reset_peak()
            return tracemalloc.get_traced_memory()[0]

        def finish(baseline: int, ok: bool, value: Any) -> None:
            current, peak = tracemalloc.get_traced_memory()
            _trace_release()
            record = MemoryRecord(name, max(peak - baseline, 0), current - baseline)
            lg.log(
                level,
                "%s peak memory %s (net %s)",
                name,
                _format_bytes(record.peak_bytes),
                _format_bytes(record.net_bytes, signed=True),
            )
            _safe_callback(lg, callback, record, name)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)


# ------------------------------------------------------------------------- profile


def _another_profiler_active() -> bool:
    # Before 3.12 enabling a second cProfile silently replaces the first one.
    return sys.version_info < (3, 12) and sys.getprofile() is not None


@overload
def profile(func: F, /) -> F: ...
@overload
def profile(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    sort: str = "cumulative",
    limit: int | None = 20,
    callback: Callable[[pstats.Stats], None] | None = None,
) -> Callable[[F], F]: ...
def profile(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    sort: str = "cumulative",
    limit: int | None = 20,
    callback: Callable[[pstats.Stats], None] | None = None,
) -> Any:
    """Profile each call with `cProfile` and log the top rows.

    If another profiler is already active (Python 3.12+ raises ``ValueError`` when
    profilers nest), a warning is logged once and the function simply runs unprofiled.

    Note:
        For ``async def`` functions the profiler is enabled for the whole awaited
        coroutine, so work the event loop does for *other* tasks while this one is
        suspended is included in the results.

    Args:
        func: The function, when used as a bare ``@profile``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level.
        sort: A `pstats.SortKey` value such as ``"cumulative"`` or ``"tottime"``.
        limit: Number of rows to log; ``None`` logs all.
        callback: Called with the `pstats.Stats` of every profiled call.

    Returns:
        The decorated function, with its signature preserved.

    Raises:
        ValueError: If ``sort`` is not a valid `pstats.SortKey`.

    Example:
        >>> @profile(sort="tottime", limit=10)
        ... def work(): ...
    """
    sort_key = pstats.SortKey(sort)  # validate eagerly

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        warned = False

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> cProfile.Profile | None:
            nonlocal warned
            profiler = cProfile.Profile()
            try:
                if _another_profiler_active():
                    raise ValueError("another profiler is active")
                profiler.enable()
            except ValueError:
                if not warned:
                    warned = True
                    lg.warning("Another profiler is active; running %s unprofiled", name)
                return None
            return profiler

        def finish(profiler: cProfile.Profile | None, ok: bool, value: Any) -> None:
            if profiler is None:
                return
            profiler.disable()
            stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_key)
            if lg.isEnabledFor(level):
                if limit is None:
                    stats.print_stats()
                else:
                    stats.print_stats(limit)
                lg.log(level, "Profile of %s:\n%s", name, stream.getvalue().rstrip())
            _safe_callback(lg, callback, stats, name)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)
