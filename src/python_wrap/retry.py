"""Retry, fallback and suppress decorators."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import inspect
import logging
import math
import random
import sys
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, NoReturn, Protocol, TypeVar, cast, overload

from python_wrap import _core, _fork, config
from python_wrap._core import F, LoggerLike, P, R
from python_wrap.deadline import remaining as _deadline_remaining
from python_wrap.errors import CircuitOpenError

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]

__all__ = [
    "Attempt",
    "RetryBudget",
    "RetryBudgetStats",
    "RetryState",
    "RetryStats",
    "Retrying",
    "fallback",
    "http_status",
    "retry",
    "retry_after_header",
    "retry_on_exception",
    "retry_stats",
    "retrying",
    "suppress",
]

_MISSING: Any = object()
_UNSET: Any = config.UNSET  # resolved by `_resolve_defaults`

ExceptionPredicate = Callable[[BaseException], bool] | Callable[[BaseException], Awaitable[bool]]
ResultPredicate = Callable[[Any], bool] | Callable[[Any], Awaitable[bool]]
RetryCallback = Callable[["RetryState"], None] | Callable[["RetryState"], Awaitable[None]]


@dataclass(frozen=True)
class RetryState:
    """Snapshot passed to the `on_retry` callback before each wait.

    Attributes:
        attempt: Number of the attempt that just failed (1-based).
        exception: The exception that attempt raised, or `None` when the attempt
            returned a value that `retry_if_result` rejected.
        next_delay: Seconds about to be slept before the next attempt (jitter included).
        elapsed: Seconds since the first attempt started.
        result: The rejected return value when `exception` is `None`, else `None`.
    """

    attempt: int
    exception: BaseException | None
    next_delay: float
    elapsed: float
    result: Any = None


def _validate(
    max_attempts: int | None,
    delay: float,
    backoff: float,
    max_delay: float | None,
    jitter: float,
    max_time: float | None = None,
) -> None:
    if max_attempts is not None and max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1 or None, got {max_attempts!r}")
    if delay < 0:
        raise ValueError(f"delay must be >= 0, got {delay!r}")
    if backoff < 1:
        raise ValueError(f"backoff must be >= 1, got {backoff!r}")
    if jitter < 0:
        raise ValueError(f"jitter must be >= 0, got {jitter!r}")
    if max_delay is not None and max_delay < 0:
        raise ValueError(f"max_delay must be >= 0, got {max_delay!r}")
    if max_time is not None and max_time < 0:
        raise ValueError(f"max_time must be >= 0 or None, got {max_time!r}")


def _validate_delays(delays: Sequence[float] | None) -> tuple[float, ...] | None:
    if delays is None:
        return None
    if not isinstance(delays, Sequence) or isinstance(delays, (str, bytes)) or not delays:
        raise ValueError("delays must be a nonempty sequence of finite nonnegative seconds")
    values = []
    for value in delays:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("delays must contain finite nonnegative seconds")
        try:
            normalized = float(value)
        except OverflowError as exc:
            raise ValueError("delays must contain finite nonnegative seconds") from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("delays must contain finite nonnegative seconds")
        values.append(normalized)
    return tuple(values)


def _compute_delay(
    attempt: int, delay: float, backoff: float, max_delay: float | None, jitter: float
) -> float:
    try:
        wait = delay * backoff ** (attempt - 1)
    except OverflowError:
        wait = sys.float_info.max
    if max_delay is not None:
        wait = min(wait, max_delay)
    if jitter:
        wait += random.uniform(0, jitter)
    return wait


class RetryStats:
    """Thread-safe counters for one retried function. Read them with `retry_stats`.

    Attributes:
        calls: Times the wrapped function was called (each call may make several attempts).
        attempts_total: Individual attempts made across all calls.
        retries: Times an attempt failed and another one was scheduled.
        gave_up: Calls that ended by re-raising a retryable exception, or by returning
            a result `retry_if_result` rejected (attempts, time budget, retry budget or
            `retry_if` said stop).
        budget_exhausted: Calls among `gave_up` that stopped because the shared
            `RetryBudget` refused another retry.
        total_sleep: Seconds spent waiting between attempts.
        last_exception: The most recent exception a retried attempt raised, if any.
    """

    def __init__(self) -> None:
        self._lock = _fork.ForkSafeLock()
        self.calls = 0
        self.attempts_total = 0
        self.retries = 0
        self.gave_up = 0
        self.budget_exhausted = 0
        self.total_sleep = 0.0
        self.last_exception: BaseException | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return a consistent copy of all counters as a dict."""
        with self._lock:
            return {
                "calls": self.calls,
                "attempts_total": self.attempts_total,
                "retries": self.retries,
                "gave_up": self.gave_up,
                "budget_exhausted": self.budget_exhausted,
                "total_sleep": self.total_sleep,
                "last_exception": self.last_exception,
            }

    def reset(self) -> None:
        """Set every counter back to zero."""
        with self._lock:
            self.calls = self.attempts_total = self.retries = self.gave_up = 0
            self.budget_exhausted = 0
            self.total_sleep = 0.0
            self.last_exception = None

    def __repr__(self) -> str:
        s = self.snapshot()
        return (
            f"RetryStats(calls={s['calls']}, attempts_total={s['attempts_total']}, "
            f"retries={s['retries']}, gave_up={s['gave_up']}, "
            f"budget_exhausted={s['budget_exhausted']}, total_sleep={s['total_sleep']:g})"
        )


_STATS_ATTR = "__retry_stats__"


def retry_stats(func: Callable[..., Any]) -> RetryStats | None:
    """Return the `RetryStats` of a function decorated with `retry`.

    Works through other decorators that use `functools.wraps` and on bound methods
    (the counters are shared by all instances of the class).

    Args:
        func: A function wrapped by `retry`.

    Returns:
        Its statistics, or `None` if `func` is not retried.

    Example:
        >>> @retry
        ... def f() -> None: ...
        >>> retry_stats(f).calls
        0
    """
    stats = getattr(func, _STATS_ATTR, None)
    return stats if isinstance(stats, RetryStats) else None


@dataclass(frozen=True)
class RetryBudgetStats:
    """Snapshot of a `RetryBudget`, returned by `RetryBudget.stats`.

    Attributes:
        requests: First attempts recorded in the current window.
        retries: Retries granted in the current window.
        rejected: Retries refused since creation (or the last `reset`).
        allowed: Retries the budget would still grant right now.
    """

    requests: int
    retries: int
    rejected: int
    allowed: int


_BUDGET_SLOTS = 10


class RetryBudget:
    """A retry allowance shared by many retried functions (Finagle/Envoy style).

    Every call's first attempt deposits into the budget and every retry withdraws from
    it. A retry is allowed while the retries made in the last `window` seconds stay
    within `ratio` times the requests made in that window, plus a reserve of
    `min_per_second * window` retries so that low-traffic code can still retry. When a
    dependency fails for everyone, retries are capped at about `ratio` of the traffic
    instead of multiplying it by `max_attempts`.

    One budget can be passed to any number of `retry` decorators and `retrying` blocks;
    it is thread-safe and works across threads and event loops (it never blocks). Time
    comes from `python_wrap._core.monotonic`, so `fake_clock` controls it in tests.

    Args:
        ratio: Retries allowed per request in the window (`0.1` = 10 %). `>= 0`.
        min_per_second: Retries per second always allowed regardless of traffic. `>= 0`.
        window: Length of the sliding window in seconds. `> 0`.

    Raises:
        ValueError: If an argument is out of range.

    Example:
        ```python
        from python_wrap.retry import RetryBudget, retry

        budget = RetryBudget(ratio=0.2, min_per_second=1, window=10)

        @retry(max_attempts=3, delay=0, budget=budget)
        def fetch() -> str:
            return "ok"

        assert fetch() == "ok"
        assert budget.stats.requests == 1
        ```
    """

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def __init__(
        self, ratio: float = 0.1, min_per_second: float = 10, window: float = 10.0
    ) -> None:
        if not ratio >= 0 or math.isinf(ratio):
            raise ValueError(f"ratio must be a finite number >= 0, got {ratio!r}")
        if not min_per_second >= 0 or math.isinf(min_per_second):
            raise ValueError(f"min_per_second must be a finite number >= 0, got {min_per_second!r}")
        if not window > 0 or math.isinf(window):
            raise ValueError(f"window must be a finite number > 0, got {window!r}")
        self.ratio = float(ratio)
        self.min_per_second = float(min_per_second)
        self.window = float(window)
        self._reserve = self.min_per_second * self.window
        self._width = self.window / _BUDGET_SLOTS
        self._lock = _fork.ForkSafeLock()
        self._epochs = [-1] * _BUDGET_SLOTS
        self._requests = [0] * _BUDGET_SLOTS
        self._retries = [0] * _BUDGET_SLOTS
        self._rejected = 0
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._epochs = [-1] * _BUDGET_SLOTS
        self._requests = [0] * _BUDGET_SLOTS
        self._retries = [0] * _BUDGET_SLOTS
        self._rejected = 0

    def _slot(self) -> tuple[int, int]:
        """Index and epoch of the current slot; clears it if it held an older epoch."""
        epoch = int(_core.monotonic() // self._width)
        index = epoch % _BUDGET_SLOTS
        if self._epochs[index] != epoch:
            self._epochs[index] = epoch
            self._requests[index] = 0
            self._retries[index] = 0
        return index, epoch

    def _totals(self, epoch: int) -> tuple[int, int]:
        oldest = epoch - _BUDGET_SLOTS
        requests = retries = 0
        for i in range(_BUDGET_SLOTS):
            if self._epochs[i] > oldest:
                requests += self._requests[i]
                retries += self._retries[i]
        return requests, retries

    def _available(self, requests: int, retries: int) -> float:
        return self.ratio * requests + self._reserve - retries

    def deposit(self) -> None:
        """Record one request (a first attempt). `retry` calls this for you."""
        with self._lock:
            index, _ = self._slot()
            self._requests[index] += 1

    def try_withdraw(self) -> bool:
        """Take one retry from the budget.

        `retry` calls this before each retry; call it yourself only in a hand-written
        loop.

        Returns:
            `True` if the retry is allowed (and was recorded), `False` if the budget is
            exhausted (recorded as rejected).
        """
        with self._lock:
            index, epoch = self._slot()
            if self._available(*self._totals(epoch)) >= 1:
                self._retries[index] += 1
                return True
            self._rejected += 1
            return False

    @property
    def stats(self) -> RetryBudgetStats:
        """A consistent `RetryBudgetStats` snapshot of the current window."""
        with self._lock:
            _, epoch = self._slot()
            requests, retries = self._totals(epoch)
            allowed = max(math.floor(self._available(requests, retries)), 0)
            return RetryBudgetStats(requests, retries, self._rejected, allowed)

    def reset(self) -> None:
        """Forget all recorded requests, retries and rejections."""
        with self._lock:
            self._epochs = [-1] * _BUDGET_SLOTS
            self._requests = [0] * _BUDGET_SLOTS
            self._retries = [0] * _BUDGET_SLOTS
            self._rejected = 0

    def __repr__(self) -> str:
        return (
            f"RetryBudget(ratio={self.ratio:g}, min_per_second={self.min_per_second:g}, "
            f"window={self.window:g})"
        )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _parse_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            import email.utils  # only HTTP-date headers need it; keeps import fast

            moment = email.utils.parsedate_to_datetime(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            seconds = moment.timestamp() - time.time()
    if math.isnan(seconds) or math.isinf(seconds):
        return None
    return max(seconds, 0.0)


def _header(headers: Any, name: str) -> Any:
    """Read a header from a mapping or pairs, case-insensitively."""
    if headers is None:
        return None
    try:
        items = headers.items() if hasattr(headers, "items") else headers
        for key, value in items:
            if str(key).casefold() == name.casefold():
                return value
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def retry_after_header(value: Any, *, json_key: str | None = None) -> float | None:
    """Extract a server-requested wait for `delay_from=`, from an error **or a result**.

    Accepts either an exception or a returned response-like object, so result-based
    retries (``retry_if_result=``) can honour the server's wait too. Looks at:

    * a ``retry_after`` attribute;
    * a ``Retry-After`` header (case-insensitive), on the object itself or on
      ``exc.response``;
    * with ``json_key=``, that key in the response's JSON body (vendor-specific APIs put
      ``retry_after`` there). A failing ``.json()`` is ignored.

    Values may be seconds or an HTTP-date; dates become seconds from now. Works with
    ``httpx``, ``requests``, ``aiohttp`` and duck-typed responses without importing them,
    and never raises.

    Args:
        value: An exception or a response-like object.
        json_key: Optional JSON field holding the wait, e.g. ``"retry_after"``.

    Returns:
        Seconds to wait (never negative), or `None` if nothing usable was found.

    Example:
        >>> class Err(Exception):
        ...     retry_after = 3
        >>> retry_after_header(Err())
        3.0
    """
    try:
        found = _parse_seconds(getattr(value, "retry_after", None))
        if found is not None:
            return found
        headers = getattr(value, "headers", None)
        if headers is None:
            response = getattr(value, "response", None)
            headers = getattr(response, "headers", None)
        found = _parse_seconds(_header(headers, "Retry-After"))
        if found is not None:
            return found
        if json_key:
            response = getattr(value, "response", None)
            target = value if hasattr(value, "json") else response
            body = getattr(target, "json", None)
            if callable(body):
                try:
                    payload = body()
                except Exception:
                    payload = None
                if inspect.iscoroutine(payload):
                    payload.close()  # sync helper cannot await an async response decoder
                    return None
                if isinstance(payload, dict):
                    return _parse_seconds(payload.get(json_key))
        return None
    except Exception:
        return None


def http_status(*codes: int) -> Callable[[Any], bool]:
    """Build a ``retry_if_result`` predicate that matches HTTP status codes.

    Reads a duck-typed ``.status_code`` or ``.status`` attribute, so it works with
    ``requests``, ``httpx``, ``aiohttp`` and simple stubs. A value without either
    attribute never matches.

    Args:
        *codes: Status codes to retry, e.g. ``429, 500, 502, 503, 504``.

    Returns:
        A predicate for ``retry_if_result=``.

    Raises:
        ValueError: If no code is given or a code is outside 100-599.

    Example:
        ```python
        from python_wrap import http_status, retry, retry_after_header

        @retry(retry_if_result=http_status(429, 503), delay_from=retry_after_header)
        def call(url):
            return client.get(url)
        ```
    """
    if not codes:
        raise ValueError("http_status() needs at least one status code")
    wanted = frozenset(codes)
    for code in wanted:
        if not 100 <= code <= 599:
            raise ValueError(f"not an HTTP status code: {code!r}")

    def matches(result: Any) -> bool:
        status = getattr(result, "status_code", None)
        if status is None:
            status = getattr(result, "status", None)
        return status in wanted

    return matches


# Never retried, even with ``on=BaseException``: retrying these hangs shutdown/cancellation.
_NEVER_RETRY = (
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
    CircuitOpenError,
)


def _parse_hint(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None
    return max(float(value), 0.0)


def _resolve_defaults(**given: Any) -> dict[str, Any]:
    """Fill arguments left unset from `python_wrap.config` (read at decoration time)."""
    return {k: config.retry_default(k, v, _RETRY_FALLBACK[k]) for k, v in given.items()}


_RETRY_FALLBACK: dict[str, Any] = {
    "max_attempts": 3,
    "delay": 1.0,
    "backoff": 1.0,
    "max_delay": None,
    "jitter": 0.0,
    "max_time": None,
    "log_level": logging.WARNING,
    "budget": None,
}


def _async_hooks(**hooks: Callable[..., Any] | None) -> list[str]:
    """Names of the given hooks that are async callables."""
    return [name for name, hook in hooks.items() if hook is not None and _core.is_async(hook)]


class _Engine:
    """The retry state machine shared by `retry` and `retrying`."""

    def __init__(
        self,
        name: str,
        *,
        max_attempts: int | None,
        delay: float,
        backoff: float,
        max_delay: float | None,
        jitter: float,
        max_time: float | None,
        on: ExceptionTypes,
        retry_if: ExceptionPredicate | None,
        retry_if_result: ResultPredicate | None,
        on_retry: RetryCallback | None,
        delay_from: Callable[[Any], float | None] | None,
        budget: RetryBudget | None,
        logger: LoggerLike,
        log_level: int | None,
        log_exc_info: bool,
        stats: RetryStats,
        delays: Sequence[float] | None = None,
        log_values: bool = True,
    ) -> None:
        _validate(max_attempts, delay, backoff, max_delay, jitter, max_time)
        _check_budget(budget)
        self.name = name
        self.max_attempts = max_attempts
        self.delay = delay
        self.delays = _validate_delays(delays)
        self.backoff = backoff
        self.max_delay = max_delay
        self.jitter = jitter
        self.max_time = max_time
        self.on = on
        self.retry_if = retry_if
        self.retry_if_result = retry_if_result
        self.on_retry = on_retry
        if delay_from is not None and _core.is_async(delay_from):
            raise TypeError("delay_from must be synchronous")
        self.delay_from = delay_from
        self.budget = budget
        self.log = _core.get_logger(logger)
        self.log_values = log_values
        self.log_level = log_level
        self.log_exc_info = log_exc_info
        self.stats = stats
        if budget is not None:  # keeps the budget check off the no-budget hot path
            self.start_call = self._start_call_with_budget  # type: ignore[method-assign]
        self.async_hooks = _async_hooks(
            retry_if=retry_if, retry_if_result=retry_if_result, on_retry=on_retry
        )

    def limit(self) -> int | None:
        """Attempts allowed for this call, honouring the test override at call time."""
        cap = _core.overrides.max_attempts
        if cap is None:
            return self.max_attempts
        return cap if self.max_attempts is None else min(self.max_attempts, cap)

    def start_call(self) -> float:
        with self.stats._lock:
            self.stats.calls += 1
        return _core.monotonic()

    def _start_call_with_budget(self) -> float:
        with self.stats._lock:
            self.stats.calls += 1
        self.budget.deposit()  # type: ignore[union-attr]  # only installed with a budget
        return _core.monotonic()

    def start_attempt(self) -> None:
        with self.stats._lock:
            self.stats.attempts_total += 1

    def success(self, attempts: int) -> None:
        _core.emit("retry.succeeded", self.name, attempts=attempts)

    def slept(self, seconds: float) -> None:
        with self.stats._lock:
            self.stats.total_sleep += seconds

    def check_sync(self, *hooks: str) -> None:
        """Refuse async hooks where nothing can await them."""
        bad = [h for h in hooks if h in self.async_hooks]
        if bad:
            raise TypeError(
                f"{self.name}: {', '.join(f'{h}=' for h in bad)} is an async function, which "
                "only an async retry can await: decorate an `async def` function, or use "
                "`async for attempt in retrying(...)` with `async with attempt:`"
            )

    def _wait_for(self, attempt: int, exc: BaseException | None, result: Any = None) -> float:
        if self.delay_from is not None:
            try:
                # A result-based retry passes the rejected value, so a response-like
                # object with Retry-After (or retry_after in its JSON) is honoured.
                raw_hint = self.delay_from(exc if exc is not None else result)
                if inspect.iscoroutine(raw_hint):
                    raw_hint.close()
                    hint = None
                else:
                    hint = _parse_hint(raw_hint)
            except Exception as hint_error:
                with contextlib.suppress(Exception):
                    self.log.debug(
                        "%s: delay_from raised (%s); using backoff",
                        self.name,
                        type(hint_error).__name__,
                        exc_info=self.log_values,
                    )
                hint = None
            if hint is not None:
                return hint if self.max_delay is None else min(hint, self.max_delay)
        if self.delays is not None:
            delay = self.delays[min(attempt - 1, len(self.delays) - 1)]
            return _compute_delay(1, delay, 1.0, self.max_delay, self.jitter)
        return _compute_delay(attempt, self.delay, self.backoff, self.max_delay, self.jitter)

    def _plan(
        self,
        attempt: int,
        exc: BaseException | None,
        wanted: bool,
        started: float,
        result: Any = None,
    ) -> float | None:
        """The wait before the next attempt, or None to stop (counted and emitted)."""
        wait: float | None = None
        denied = False
        limit = self.limit()
        if wanted and (limit is None or attempt < limit):
            wait = self._wait_for(attempt, exc, result)
            if self.max_time is not None and _core.monotonic() - started + wait > self.max_time:
                wait = None
            left = _deadline_remaining()
            if wait is not None and left is not None and wait > left:
                wait = None  # never sleep past the surrounding `deadline`
            if wait is not None and self.budget is not None and not self.budget.try_withdraw():
                wait = None
                denied = True
        if wait is None:
            with self.stats._lock:
                self.stats.gave_up += 1
                if denied:
                    self.stats.budget_exhausted += 1
            if denied:
                _core.emit("retry.budget_exhausted", self.name, attempts=attempt, exception=exc)
            _core.emit("retry.gave_up", self.name, attempts=attempt, exception=exc)
            return None
        with self.stats._lock:
            self.stats.retries += 1
        return wait

    def _state(
        self, attempt: int, exc: BaseException | None, result: Any, wait: float, started: float
    ) -> RetryState:
        return RetryState(attempt, exc, wait, _core.monotonic() - started, result)

    def _announce(self, attempt: int, exc: BaseException | None, result: Any, wait: float) -> None:
        if self.log_level is not None:
            limit = self.limit()
            if self.log_values:
                what = repr(exc) if exc is not None else f"returned {result!r}"
            else:
                what = (
                    type(exc).__name__ if exc is not None else f"returned {type(result).__name__}"
                )
            self.log.log(
                self.log_level,
                "%s failed (attempt %d/%s): %s; retrying in %.3fs",
                self.name,
                attempt,
                "inf" if limit is None else limit,
                what,
                wait,
                exc_info=(
                    (type(exc), exc, exc.__traceback__)
                    if self.log_values and self.log_exc_info and exc is not None
                    else None
                ),
            )
        _core.emit(
            "retry.attempt_failed", self.name, attempt=attempt, exception=exc, next_delay=wait
        )

    def _record(self, exc: BaseException) -> None:
        with self.stats._lock:
            self.stats.last_exception = exc

    def _run_hook(self, state: RetryState) -> None:
        """Run `on_retry`; a failing hook must never replace the original outcome."""
        if self.on_retry is None:
            return
        try:
            self.on_retry(state)
        except Exception as hook_error:
            self._hook_failure(hook_error)

    def _hook_failure(self, error: Exception) -> None:
        with contextlib.suppress(Exception):
            self.log.error(
                "%s: on_retry hook failed (%s); the original error/result is kept",
                self.name,
                type(error).__name__,
                exc_info=self.log_values,
            )

    async def _arun_hook(self, state: RetryState) -> None:
        """`_run_hook` for async callers: an async hook is awaited, failures swallowed."""
        if self.on_retry is None:
            return
        try:
            await _maybe_await(self.on_retry(state))
        except Exception as hook_error:
            self._hook_failure(hook_error)

    # -- sync decisions -------------------------------------------------------------------
    def failure(self, attempt: int, exc: BaseException, started: float) -> float | None:
        """Decide after a failed attempt: the wait in seconds, or None to re-raise."""
        if isinstance(exc, _NEVER_RETRY):
            return None
        self.check_sync("retry_if", "on_retry")
        self._record(exc)
        wanted = self.retry_if is None or bool(self.retry_if(exc))
        wait = self._plan(attempt, exc, wanted, started)
        if wait is not None:
            self._run_hook(self._state(attempt, exc, None, wait, started))
            self._announce(attempt, exc, None, wait)
        return wait

    def rejects(self, result: Any) -> bool:
        """True if `retry_if_result` asks for another attempt (sync callers)."""
        if self.retry_if_result is None:
            return False
        self.check_sync("retry_if_result")
        return bool(self.retry_if_result(result))

    def result_failure(self, attempt: int, result: Any, started: float) -> float | None:
        """Decide after a rejected result: the wait, or None to return it as is."""
        self.check_sync("on_retry")
        wait = self._plan(attempt, None, True, started, result)
        if wait is not None:
            self._run_hook(self._state(attempt, None, result, wait, started))
            self._announce(attempt, None, result, wait)
        return wait

    # -- async decisions ------------------------------------------------------------------
    async def afailure(self, attempt: int, exc: BaseException, started: float) -> float | None:
        """`failure` for async callers: async hooks are awaited."""
        if isinstance(exc, _NEVER_RETRY):
            return None
        self._record(exc)
        wanted = self.retry_if is None or bool(await _maybe_await(self.retry_if(exc)))
        wait = self._plan(attempt, exc, wanted, started)
        if wait is not None:
            await self._arun_hook(self._state(attempt, exc, None, wait, started))
            self._announce(attempt, exc, None, wait)
        return wait

    async def arejects(self, result: Any) -> bool:
        """`rejects` for async callers."""
        if self.retry_if_result is None:
            return False
        return bool(await _maybe_await(self.retry_if_result(result)))

    async def aresult_failure(self, attempt: int, result: Any, started: float) -> float | None:
        """`result_failure` for async callers."""
        wait = self._plan(attempt, None, True, started, result)
        if wait is not None:
            state = self._state(attempt, None, result, wait, started)
            await self._arun_hook(state)
            self._announce(attempt, None, result, wait)
        return wait


def _check_budget(budget: Any) -> None:
    if budget is not None and not isinstance(budget, RetryBudget):
        raise TypeError(f"budget must be a RetryBudget or None, got {budget!r}")


@overload
def retry(
    func: F,
    /,
    *,
    max_attempts: int | None = ...,
    delay: float = ...,
    backoff: float = ...,
    max_delay: float | None = ...,
    jitter: float = ...,
    max_time: float | None = ...,
    on: ExceptionTypes = ...,
    retry_if: ExceptionPredicate | None = ...,
    retry_if_result: ResultPredicate | None = ...,
    on_retry: RetryCallback | None = ...,
    delay_from: Callable[[Any], float | None] | None = ...,
    delays: Sequence[float] | None = ...,
    budget: RetryBudget | None = ...,
    logger: LoggerLike = ...,
    log_level: int | None = ...,
    log_exc_info: bool = ...,
    log_values: bool = ...,
) -> F: ...
@overload
def retry(
    func: None = None,
    /,
    *,
    max_attempts: int | None = ...,
    delay: float = ...,
    backoff: float = ...,
    max_delay: float | None = ...,
    jitter: float = ...,
    max_time: float | None = ...,
    on: ExceptionTypes = ...,
    retry_if: ExceptionPredicate | None = ...,
    retry_if_result: ResultPredicate | None = ...,
    on_retry: RetryCallback | None = ...,
    delay_from: Callable[[Any], float | None] | None = ...,
    delays: Sequence[float] | None = ...,
    budget: RetryBudget | None = ...,
    logger: LoggerLike = ...,
    log_level: int | None = ...,
    log_exc_info: bool = ...,
    log_values: bool = ...,
) -> Callable[[F], F]: ...
def retry(
    func: Callable[..., Any] | None = None,
    /,
    *,
    max_attempts: int | None = _UNSET,
    delay: float = _UNSET,
    backoff: float = _UNSET,
    max_delay: float | None = _UNSET,
    jitter: float = _UNSET,
    max_time: float | None = _UNSET,
    on: ExceptionTypes = Exception,
    retry_if: ExceptionPredicate | None = None,
    retry_if_result: ResultPredicate | None = None,
    on_retry: RetryCallback | None = None,
    delay_from: Callable[[Any], float | None] | None = None,
    delays: Sequence[float] | None = None,
    budget: RetryBudget | None = _UNSET,
    logger: LoggerLike = None,
    log_level: int | None = _UNSET,
    log_exc_info: bool = False,
    log_values: bool = True,
) -> Any:
    """Retry a function when it raises (or returns a bad value), with optional backoff.

    Works bare (`@retry`), with options (`@retry(max_attempts=5)`) and as a plain
    call (`retry(func, max_attempts=5)`). Async functions are awaited and slept
    with `asyncio` so the event loop is never blocked.

    The wait before attempt `n + 1` is
    `min(delay * backoff ** (n - 1), max_delay) + random.uniform(0, jitter)`.
    The last exception is re-raised unchanged, with its original traceback, once
    attempts, the time budget or the retry budget are exhausted.

    With `retry_if_result`, a returned value for which the predicate is true is treated
    as a failure and retried. When no retry is left, that last value is **returned**
    (no exception is raised), so check it if it matters.

    For `async def` functions, `retry_if`, `retry_if_result` and `on_retry` may be
    `async def` callables; they are awaited. A sync function given an async callable
    raises `TypeError` when decorated.

    `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are
    never retried, even with `on=BaseException` or when listed in `on`. Cancelling an
    async call during the backoff sleep cancels it immediately.

    Arguments you leave out fall back to `python_wrap.config.configure(retry={...})`
    (read when the decorator is applied), then to the values shown here. Inside a
    `python_wrap.deadline` block, it never sleeps past the time remaining: if the next
    wait would outlast the deadline the last exception is re-raised instead.

    Statistics are available through `retry_stats`. The events `retry.attempt_failed`,
    `retry.succeeded`, `retry.gave_up` and `retry.budget_exhausted` are emitted for
    `python_wrap.events`. Under `python_wrap.testing`, an `overrides.max_attempts` cap
    lowers `max_attempts`.

    Args:
        func: The function to wrap (when used without parentheses).
        max_attempts: Total number of calls allowed, or `None` for no limit.
        delays: Optional nonempty sequence of finite nonnegative waits. Failure n
            uses entry n (one-based), repeating the last entry after exhaustion.
            Overrides delay/backoff; max_delay caps before jitter. A valid delay_from
            hint takes priority. Does not change max_attempts. Copied at creation.
        delay: Base wait in seconds before the second attempt.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Upper bound for the backoff wait (before jitter is added); also caps
            waits returned by `delay_from`.
        jitter: Up to this many random extra seconds are added to each wait.
        max_time: Total time budget in seconds, measured from the first attempt. If the
            next wait would run past it, the last exception is re-raised instead.
        on: Exception type(s) that trigger a retry; anything else propagates at once.
        retry_if: Extra predicate on the exception; it is retried only if this returns
            True. May be async for async functions.
        retry_if_result: Predicate on the return value; True means "retry". When no
            retry is left the last value is returned. May be async for async functions.
        on_retry: Called with a `RetryState` before each wait. May be async for async
            functions. An exception raised by the hook is logged and swallowed: it never
            replaces the original error or rejected result.
        delay_from: Called with the exception, or with the rejected return value when
            ``retry_if_result`` scheduled the retry; a number is the exact wait for this
            attempt (no backoff, no jitter; still capped by `max_delay` and `max_time`),
            `None` means use the normal backoff. See `retry_after_header`.
        budget: A `RetryBudget` shared with other retried code. When it refuses a retry
            the last exception is re-raised (or the last result returned) at once.
        logger: Logger (or logger name) for the per-retry message.
        log_level: Level of the per-retry message, or `None` to not log.
        log_values: False logs only exception/result type names, including hook/hint
            failures, and suppresses log_exc_info. Events, stats and callbacks still
            receive original objects; function names and custom loggers are caller-owned.
        log_exc_info: Attach the traceback to the per-retry log record.

    Returns:
        The wrapped function, or a decorator when `func` is omitted.

    Raises:
        ValueError: If an option is out of range (raised when decorating).
        TypeError: If `budget` is not a `RetryBudget`, or a sync function is given an
            async `retry_if`, `retry_if_result` or `on_retry` (raised when decorating).

    Example:
        >>> @retry(max_attempts=5, delay=0.5, backoff=2, on=ConnectionError)
        ... def fetch() -> str: ...
    """
    ds = _resolve_defaults(
        max_attempts=max_attempts,
        delay=delay,
        backoff=backoff,
        max_delay=max_delay,
        jitter=jitter,
        max_time=max_time,
        log_level=log_level,
        budget=budget,
    )
    max_attempts, delay, backoff = ds["max_attempts"], ds["delay"], ds["backoff"]
    max_delay, jitter, max_time, log_level = (
        ds["max_delay"],
        ds["jitter"],
        ds["max_time"],
        ds["log_level"],
    )
    budget = ds["budget"]
    # Validate eagerly so a bad option fails at decoration time, even with no function yet.
    _validate(max_attempts, delay, backoff, max_delay, jitter, max_time)
    _check_budget(budget)
    delays = _validate_delays(delays)

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        stats = RetryStats()
        engine = _Engine(
            _core.qualname(f),
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            max_time=max_time,
            on=on,
            retry_if=retry_if,
            retry_if_result=retry_if_result,
            on_retry=on_retry,
            delay_from=delay_from,
            delays=delays,
            budget=budget,
            logger=logger,
            log_level=log_level,
            log_exc_info=log_exc_info,
            log_values=log_values,
            stats=stats,
        )
        check_result = retry_if_result is not None

        if _core.is_async(f):
            af = cast("Callable[..., Coroutine[Any, Any, Any]]", f)

            @functools.wraps(af)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = engine.start_call()
                attempt = 1
                while True:
                    engine.start_attempt()
                    try:
                        result = await af(*args, **kwargs)
                    except on as exc:
                        wait = await engine.afailure(attempt, exc, started)
                        if wait is None:
                            raise
                    else:
                        if not check_result or not await engine.arejects(result):
                            engine.success(attempt)
                            return result
                        wait = await engine.aresult_failure(attempt, result, started)
                        if wait is None:
                            return result
                    await _core.async_sleep(wait)
                    engine.slept(wait)
                    attempt += 1

            setattr(async_wrapper, _STATS_ATTR, stats)
            return cast("Callable[P, R]", async_wrapper)

        engine.check_sync(*engine.async_hooks)

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = engine.start_call()
            attempt = 1
            while True:
                engine.start_attempt()
                try:
                    result = f(*args, **kwargs)
                except on as exc:
                    wait = engine.failure(attempt, exc, started)
                    if wait is None:
                        raise
                else:
                    if not check_result or not engine.rejects(result):
                        engine.success(attempt)
                        return result
                    wait = engine.result_failure(attempt, result, started)
                    if wait is None:
                        return result
                _core.sleep(wait)
                engine.slept(wait)
                attempt += 1

        setattr(wrapper, _STATS_ATTR, stats)
        return wrapper

    return decorate if func is None else decorate(func)


class _Run:
    def __init__(self, engine: _Engine) -> None:
        self.engine = engine
        self.started = engine.start_call()


class Attempt:
    """One attempt of a `retrying` loop; use it as a context manager.

    Exceptions that `retry` would retry are swallowed by `with attempt:` (the loop then
    waits and continues); other exceptions, and the failure of the final attempt,
    propagate. In an `async for` loop, use `async with attempt:` when `retry_if`,
    `retry_if_result` or `on_retry` are async callables (plain `with` works otherwise).

    With `retry_if_result`, store the value the block produced in `attempt.result`; the
    predicate is checked when the block ends and a rejected value starts another
    attempt. When no retry is left the loop simply ends, leaving the last value in
    your variable (and in `attempt.result`).

    Attributes:
        number: 1-based number of this attempt.
    """

    def __init__(self, number: int, run: _Run) -> None:
        self.number = number
        self._run = run
        self._wait: float | None = None
        self._gave_up = False
        self._result: Any = _MISSING

    @property
    def result(self) -> Any:
        """The value checked by `retry_if_result` (`None` until you set it)."""
        return None if self._result is _MISSING else self._result

    @result.setter
    def result(self, value: Any) -> None:
        self._result = value

    def __enter__(self) -> Attempt:
        self._run.engine.start_attempt()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        engine = self._run.engine
        if exc is None:
            if self._result is not _MISSING and engine.rejects(self._result):
                self._wait = engine.result_failure(self.number, self._result, self._run.started)
                self._gave_up = self._wait is None
            return False
        if not isinstance(exc, engine.on):
            return False
        self._wait = engine.failure(self.number, exc, self._run.started)
        return self._wait is not None

    async def __aenter__(self) -> Attempt:
        self._run.engine.start_attempt()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        engine = self._run.engine
        if exc is None:
            if self._result is not _MISSING and await engine.arejects(self._result):
                started = self._run.started
                self._wait = await engine.aresult_failure(self.number, self._result, started)
                self._gave_up = self._wait is None
            return False
        if not isinstance(exc, engine.on):
            return False
        self._wait = await engine.afailure(self.number, exc, self._run.started)
        return self._wait is not None


class Retrying:
    """Iterable of `Attempt` objects returned by `retrying`.

    Iterate it with `for` (sync, sleeps with `time.sleep`) or `async for` (sleeps with
    `asyncio`). Each iteration over a `Retrying` is an independent run.
    """

    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    @property
    def stats(self) -> RetryStats:
        """Counters accumulated over all runs of this object."""
        return self._engine.stats

    def __iter__(self) -> Iterator[Attempt]:
        run = _Run(self._engine)
        number = 1
        while True:
            attempt = Attempt(number, run)
            yield attempt
            wait = attempt._wait
            if wait is None:
                if not attempt._gave_up:
                    run.engine.success(number)
                return
            _core.sleep(wait)
            run.engine.slept(wait)
            number += 1

    async def __aiter__(self) -> AsyncIterator[Attempt]:
        run = _Run(self._engine)
        number = 1
        while True:
            attempt = Attempt(number, run)
            yield attempt
            wait = attempt._wait
            if wait is None:
                if not attempt._gave_up:
                    run.engine.success(number)
                return
            await _core.async_sleep(wait)
            run.engine.slept(wait)
            number += 1


def retrying(
    *,
    max_attempts: int | None = _UNSET,
    delay: float = _UNSET,
    backoff: float = _UNSET,
    max_delay: float | None = _UNSET,
    jitter: float = _UNSET,
    max_time: float | None = _UNSET,
    on: ExceptionTypes = Exception,
    retry_if: ExceptionPredicate | None = None,
    retry_if_result: ResultPredicate | None = None,
    on_retry: RetryCallback | None = None,
    delay_from: Callable[[Any], float | None] | None = None,
    delays: Sequence[float] | None = None,
    budget: RetryBudget | None = _UNSET,
    logger: LoggerLike = None,
    log_level: int | None = _UNSET,
    log_exc_info: bool = False,
    log_values: bool = True,
    name: str = "retrying",
) -> Retrying:
    """Retry an inline block of code instead of a whole function.

    Takes the same options as `retry` and behaves identically (backoff, jitter,
    `max_time`, `delay_from`, `budget`, cancellation rules, events, test overrides). Put
    the risky code in `with attempt:`: a retryable exception is swallowed and the loop
    waits and continues; success ends the loop; the final failure propagates.

    Use `for` in sync code and `async for` in async code. Do not use `for` inside a
    coroutine, it would block the event loop while waiting. Async `retry_if`,
    `retry_if_result` or `on_retry` callables need `async for` and `async with attempt:`;
    with plain `with attempt:` they raise `TypeError`.

    For `retry_if_result`, assign the block's value to `attempt.result`; a rejected
    value is retried, and when no retry is left the loop ends normally with the last
    value (like `retry`, nothing is raised).

    Args:
        max_attempts: Total attempts allowed, or `None` for no limit.
        delays: Optional nonempty sequence of finite nonnegative waits. Failure n
            uses entry n (one-based), repeating the last entry after exhaustion.
            Overrides delay/backoff; max_delay caps before jitter. A valid delay_from
            hint takes priority. Does not change max_attempts. Copied at creation.
        delay: Base wait in seconds before the second attempt.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Upper bound for the backoff wait (before jitter).
        jitter: Up to this many random extra seconds are added to each wait.
        max_time: Total time budget in seconds from the first attempt.
        on: Exception type(s) that trigger a retry.
        retry_if: Extra predicate on the exception; retry only if it returns True.
        retry_if_result: Predicate on `attempt.result`; True means "retry".
        on_retry: Called with a `RetryState` before each wait; a failing hook is logged
            and swallowed.
        delay_from: Exact wait derived from the exception or rejected result, or `None`
            for backoff.
        budget: A shared `RetryBudget`; when it refuses, the loop stops retrying.
        logger: Logger (or logger name) for the per-retry message.
        log_level: Level of the per-retry message, or `None` to not log.
        log_values: False logs only exception/result type names, including hook/hint
            failures, and suppresses log_exc_info. Events, stats and callbacks still
            receive original objects; function names and custom loggers are caller-owned.
        log_exc_info: Attach the traceback to the per-retry log record.
        name: Name used in log messages and events.

    Returns:
        A `Retrying` object to iterate over.

    Raises:
        ValueError: If an option is out of range.
        TypeError: If `budget` is not a `RetryBudget`.

    Example:
        >>> for attempt in retrying(max_attempts=3, delay=0.1, on=ConnectionError):
        ...     with attempt:
        ...         do_work()  # doctest: +SKIP
    """
    ds = _resolve_defaults(
        max_attempts=max_attempts,
        delay=delay,
        backoff=backoff,
        max_delay=max_delay,
        jitter=jitter,
        max_time=max_time,
        log_level=log_level,
        budget=budget,
    )
    max_attempts, delay, backoff = ds["max_attempts"], ds["delay"], ds["backoff"]
    max_delay, jitter, max_time, log_level = (
        ds["max_delay"],
        ds["jitter"],
        ds["max_time"],
        ds["log_level"],
    )
    return Retrying(
        _Engine(
            name,
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            max_time=max_time,
            on=on,
            retry_if=retry_if,
            retry_if_result=retry_if_result,
            on_retry=on_retry,
            delay_from=delay_from,
            delays=delays,
            budget=ds["budget"],
            logger=logger,
            log_level=log_level,
            log_exc_info=log_exc_info,
            log_values=log_values,
            stats=RetryStats(),
        )
    )


def retry_on_exception(
    *exceptions: type[BaseException],
    max_attempts: int | None = 3,
    delay: float = 0.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[RetryState], None] | None = None,
    logger: LoggerLike = None,
) -> Callable[[F], F]:
    """Deprecated alias for ``retry(on=exceptions or Exception, ...)``.

    Args:
        *exceptions: Exception types to retry on (default ``Exception``).
        max_attempts: Total calls allowed, or ``None`` for no limit.
        delay: Base wait in seconds between attempts.
        backoff: Multiplier applied to the wait after each failure.
        max_delay: Upper bound for the wait.
        jitter: Random extra seconds added to each wait.
        retry_if: Extra predicate deciding whether to retry.
        on_retry: Callback invoked before each wait.
        logger: Logger (or name) for retry messages.

    Returns:
        A decorator. Applying it emits a ``DeprecationWarning``.

    Raises:
        ValueError: If an option is out of range.

    Example:
        >>> @retry_on_exception(ValueError)  # use retry(on=ValueError) instead
        ... def parse() -> None: ...
    """
    on: ExceptionTypes = exceptions or Exception
    _validate(max_attempts, delay, backoff, max_delay, jitter)

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        warnings.warn(
            "retry_on_exception is deprecated; use retry(on=...) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return retry(
            f,
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            on=on,
            retry_if=retry_if,
            on_retry=on_retry,
            logger=logger,
        )

    return cast("Callable[[F], F]", decorate)


class _Decorator(Protocol[T_co]):
    """A decorator that maps ``f`` to ``f`` with ``T_co`` added to the result type."""

    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, R]], /
    ) -> Callable[P, Coroutine[Any, Any, R | T_co]]: ...
    @overload
    def __call__(self, func: Callable[P, R], /) -> Callable[P, R | T_co]: ...


def _make_catcher(
    on: ExceptionTypes,
    on_error: Callable[[Callable[..., Any], BaseException], Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        if _core.is_async(f):

            @functools.wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await f(*args, **kwargs)
                except on as exc:
                    return on_error(f, exc)

            return async_wrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return f(*args, **kwargs)
            except on as exc:
                return on_error(f, exc)

        return wrapper

    return decorate


@overload
def fallback(
    value: T, /, *, on: ExceptionTypes = ..., logger: LoggerLike = ...
) -> _Decorator[T]: ...
@overload
def fallback(
    *, handler: Callable[[BaseException], T], on: ExceptionTypes = ..., logger: LoggerLike = ...
) -> _Decorator[T]: ...
def fallback(
    value: Any = _MISSING,
    /,
    *,
    handler: Callable[[BaseException], Any] | None = None,
    on: ExceptionTypes = Exception,
    logger: LoggerLike = None,
) -> Any:
    """Return a default value (or a handler's result) when the function raises.

    Args:
        value: The value to return on a matching exception.
        handler: Alternatively, a callable ``handler(exc)`` whose result is returned.
        on: Exception type(s) that trigger the fallback.
        logger: Logger (or name) for the WARNING message (logged with traceback).

    Returns:
        A decorator for sync or async functions. For async functions the handler is
        still a regular (sync) function.

    Raises:
        ValueError: If neither or both of ``value`` and ``handler`` are given.

    Example:
        >>> @fallback(0, on=KeyError)
        ... def lookup(key: str) -> int: ...
    """
    if (value is _MISSING) == (handler is None):
        raise ValueError("fallback requires exactly one of `value` or `handler`")
    log = _core.get_logger(logger)

    def on_error(f: Callable[..., Any], exc: BaseException) -> Any:
        log.warning(
            "%s raised %r; using fallback",
            _core.qualname(f),
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return handler(exc) if handler is not None else value

    return _make_catcher(on, on_error)


@overload
def suppress(  # type: ignore[overload-overlap]
    exc: type[BaseException], /, *more: type[BaseException], logger: LoggerLike = ...
) -> _Decorator[None]: ...
@overload
def suppress(
    func: Callable[P, Coroutine[Any, Any, R]], /
) -> Callable[P, Coroutine[Any, Any, R | None]]: ...
@overload
def suppress(func: Callable[P, R], /) -> Callable[P, R | None]: ...
@overload
def suppress(*, logger: LoggerLike = ...) -> _Decorator[None]: ...
def suppress(*exceptions: Any, logger: LoggerLike = None) -> Any:
    """Swallow matching exceptions; the call then returns ``None``.

    Usable bare (``@suppress``, which suppresses ``Exception``) or with exception
    types (``@suppress(KeyError)``). Suppressed errors are logged at DEBUG.

    Args:
        *exceptions: Exception types to swallow (default ``Exception``).
        logger: Logger (or name) for the DEBUG message.

    Returns:
        The wrapped function, or a decorator when exception types are given.

    Example:
        >>> @suppress(KeyError)
        ... def first(d: dict[str, int]) -> int: ...
    """
    log = _core.get_logger(logger)

    def make(on: ExceptionTypes) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def on_error(f: Callable[..., Any], exc: BaseException) -> None:
            log.debug("%s raised %r; suppressed", _core.qualname(f), exc)

        return _make_catcher(on, on_error)

    if len(exceptions) == 1 and not isinstance(exceptions[0], type) and callable(exceptions[0]):
        return make(Exception)(exceptions[0])
    return make(tuple(exceptions) or Exception)
