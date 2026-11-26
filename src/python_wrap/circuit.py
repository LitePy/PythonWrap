"""Circuit breaker decorator and class."""

from __future__ import annotations

import asyncio
import enum
import functools
import math
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Concatenate, NoReturn, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core, _fork
from python_wrap._backend import BackendMonitor
from python_wrap._core import F, LoggerLike, P, R
from python_wrap.errors import CircuitOpenError

__all__ = [
    "CircuitBreaker",
    "CircuitProtected",
    "CircuitState",
    "CircuitStats",
    "StateChangeListener",
    "circuit_breaker",
]

R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
T = TypeVar("T")

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


class CircuitState(str, enum.Enum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateChangeListener = Callable[["CircuitBreaker", CircuitState, CircuitState], None]
"""Signature of ``on_state_change``: ``listener(breaker, old_state, new_state)``."""


@dataclass(frozen=True)
class CircuitStats:
    """A point-in-time snapshot of a `CircuitBreaker`, from its ``.stats`` property.

    Attributes:
        name: The breaker's name.
        state: Current state.
        failures: Calls that failed with a counted exception (since creation or the last
            ``reset()``).
        successes: Calls that returned normally (same period).
        rejected: Calls refused with `CircuitOpenError` (same period).
        consecutive_failures: Counted failures since the last success.
        opened_at: ``_core.monotonic()`` time at which the circuit last opened, or
            ``None`` while it is closed.
        failure_rate: Share of calls in ``window`` that failed, for a breaker with
            ``failure_rate`` or ``slow_call_rate``; otherwise ``None``.
        slow_rate: Share of calls in ``window`` slower than ``slow_call_duration``
            (same condition).
    """

    name: str
    state: CircuitState
    failures: int
    successes: int
    rejected: int
    consecutive_failures: int
    opened_at: float | None
    failure_rate: float | None = None
    slow_rate: float | None = None


class CircuitBreaker:
    """Stop calling a failing dependency until it has had time to recover.

    The breaker is closed at first. After ``failure_threshold`` consecutive matching
    failures it opens and rejects calls with `CircuitOpenError`.
    After ``recovery_timeout`` seconds it goes half-open and lets up to
    ``half_open_max_calls`` trial calls run at once: ``success_threshold`` consecutive
    successful trials close it, any counted failure re-opens it. One instance can
    decorate several functions to share state.

    Exceptions matching ``on`` are counted as failures, unless they also match
    ``exclude``. Excluded and non-matching exceptions propagate to the caller without
    affecting the breaker (they count neither as a failure nor as a success).

    Each transition calls ``on_state_change(breaker, old, new)`` (outside the breaker's
    lock; a failing listener is logged, never raised) and emits the
    ``circuit.state_changed`` event. Every rejected call emits ``circuit.rejected``.

    Args:
        failure_threshold: Consecutive failures that open the circuit (>= 1).
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Concurrent trial calls allowed while half-open (>= 1).
        success_threshold: Consecutive successful trial calls needed to close a
            half-open circuit (>= 1).
        on: Exception type(s) that count as failures; others pass through uncounted.
        exclude: Exception type(s) never counted, even when they match ``on``, e.g.
            ``exclude=NotFound`` with ``on=Exception``.
        on_state_change: Listener called with ``(breaker, old_state, new_state)``.
        name: Name used in log messages, errors and events.
        logger: Logger (or name) for state transitions.
        window: Count failures inside this many seconds instead of consecutive failures.
        failure_rate: Open when this share (0 to 1) of the calls in ``window`` failed,
            once ``minimum_calls`` calls were seen. Needs ``window``; replaces
            ``failure_threshold`` (a non-default threshold with it raises ValueError).
        minimum_calls: Calls in ``window`` needed before a rate can open the breaker.
        slow_call_duration: Seconds after which a call counts as slow, success or not.
        slow_call_rate: Open when this share of the calls in ``window`` was slow (after
            ``minimum_calls``). Needs ``window`` and ``slow_call_duration``. A slow
            half-open trial re-opens the breaker.
        backend: Shared state such as `python_wrap.shared.RedisBreakerState`.
        on_backend_error: What a store error does: ``"raise"`` (default), ``"local"``
            (use this process's own breaker until the store answers) or ``"allow"``
            (admit the call, skip accounting). Defaults to ``configure(backend_errors=)``.
        retry_backend_after: Seconds to skip a failed store before trying it again.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        ```python
        from python_wrap.circuit import CircuitBreaker, CircuitState

        changes = []
        breaker = CircuitBreaker(
            failure_threshold=2,
            exclude=KeyError,
            on_state_change=lambda b, old, new: changes.append((old.value, new.value)),
            name="payments",
        )

        @breaker
        def charge(fail: bool) -> str:
            if fail:
                raise ConnectionError("down")
            return "ok"

        for _ in range(2):
            try:
                charge(True)
            except ConnectionError:
                pass
        assert breaker.state is CircuitState.OPEN
        assert changes == [("closed", "open")]
        assert breaker.stats.failures == 2
        ```
    """

    @staticmethod
    def name_for(name: str | None) -> str:
        return name or "circuit"

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        success_threshold: int = 1,
        on: ExceptionTypes = Exception,
        exclude: ExceptionTypes = (),
        on_state_change: StateChangeListener | None = None,
        name: str | None = None,
        logger: LoggerLike = None,
        window: float | None = None,
        backend: Any = None,
        on_backend_error: str | None = None,
        retry_backend_after: float = 1.0,
        failure_rate: float | None = None,
        minimum_calls: int = 10,
        slow_call_duration: float | None = None,
        slow_call_rate: float | None = None,
    ) -> None:
        if failure_rate is not None and failure_threshold != 5:
            raise ValueError("set failure_threshold or failure_rate, not both")
        for label, rate in (("failure_rate", failure_rate), ("slow_call_rate", slow_call_rate)):
            if rate is not None and (not math.isfinite(rate) or not 0 < rate <= 1):
                raise ValueError(f"{label} must be in (0, 1], got {rate!r}")
        if (slow_call_duration is None) != (slow_call_rate is None):
            raise ValueError("slow_call_duration and slow_call_rate go together")
        if slow_call_duration is not None and (
            not math.isfinite(slow_call_duration) or slow_call_duration <= 0
        ):
            raise ValueError("slow_call_duration must be positive")
        if (failure_rate is not None or slow_call_rate is not None) and window is None:
            raise ValueError("failure_rate and slow_call_rate need window=")
        if (
            isinstance(minimum_calls, bool)
            or not isinstance(minimum_calls, int)
            or minimum_calls < 1
        ):
            raise ValueError(f"minimum_calls must be >= 1, got {minimum_calls!r}")
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or failure_threshold < 1
        ):
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold!r}")
        if not math.isfinite(recovery_timeout) or recovery_timeout < 0:
            raise ValueError(f"recovery_timeout must be >= 0, got {recovery_timeout!r}")
        if (
            isinstance(half_open_max_calls, bool)
            or not isinstance(half_open_max_calls, int)
            or half_open_max_calls < 1
        ):
            raise ValueError(f"half_open_max_calls must be >= 1, got {half_open_max_calls!r}")
        if (
            isinstance(success_threshold, bool)
            or not isinstance(success_threshold, int)
            or success_threshold < 1
        ):
            raise ValueError(f"success_threshold must be >= 1, got {success_threshold!r}")
        if window is not None and (not math.isfinite(window) or window <= 0):
            raise ValueError("window must be positive")
        if backend is not None and not name:
            raise ValueError("shared breakers require an explicit name")
        self.window, self.backend = window, backend
        self._async_backend = backend is not None and _core.is_async(backend.operate)
        self._last: dict[str, Any] | None = None  # last async-backend answer, for .state
        self._monitor: BackendMonitor | None = (
            None
            if backend is None
            else BackendMonitor(
                "breaker", self.name_for(name), on_backend_error, retry_backend_after
            )
        )
        self._failure_times: deque[float] = deque()
        self.failure_rate, self.minimum_calls = failure_rate, minimum_calls
        self.slow_call_duration, self.slow_call_rate = slow_call_duration, slow_call_rate
        self._rated = failure_rate is not None or slow_call_rate is not None
        self._slots: deque[list[int]] = deque()  # [slot, calls, failures, slow] in window
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.success_threshold = success_threshold
        self.on = on
        self.exclude = exclude
        self.on_state_change = on_state_change
        self.name = name or "circuit"
        self._log = _core.get_logger(logger)
        self._lock = _fork.ForkSafeLock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trials = 0
        self._trial_successes = 0
        self._generation = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejected = 0
        # Transitions made under the lock, delivered to listeners after releasing it.
        self._pending: list[tuple[CircuitState, CircuitState]] = []
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        # Shared backends remain authoritative; never reset their state from a child.
        self._pending.clear()
        if self.backend is None:
            self._state = CircuitState.CLOSED
            self._failures = self._trials = self._trial_successes = 0
            self._total_failures = self._total_successes = self._total_rejected = 0
            self._failure_times.clear()
            self._slots.clear()
            self._generation += 1

    # -- introspection -------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """Current state (an open circuit past its timeout reports ``half_open``)."""
        data = self._try("state")
        if data is not None:
            return CircuitState(data["state"])
        with self._lock:
            self._maybe_half_open()
            state = self._state
        self._notify()
        return state

    @property
    def failure_count(self) -> int:
        """Consecutive failures counted so far."""
        data = self._try("state")
        if data is not None:
            return int(data["failures"])
        with self._lock:
            return self._failures

    @property
    def stats(self) -> CircuitStats:
        """A consistent `CircuitStats` snapshot of the counters and state."""
        data = self._try("state")
        if data is not None:
            return CircuitStats(
                name=self.name,
                state=CircuitState(data["state"]),
                failures=data["total_failures"],
                successes=data["total_successes"],
                rejected=data["total_rejected"],
                consecutive_failures=data["failures"],
                opened_at=None if data["state"] == "closed" else data["opened"] / 1000,
                **self._rates(
                    data.get("window_calls", 0),
                    data.get("window_failures", 0),
                    data.get("window_slow", 0),
                ),
            )
        with self._lock:
            self._maybe_half_open()
            snapshot = CircuitStats(
                name=self.name,
                state=self._state,
                failures=self._total_failures,
                successes=self._total_successes,
                rejected=self._total_rejected,
                consecutive_failures=self._failures,
                opened_at=None if self._state is CircuitState.CLOSED else self._opened_at,
                **self._rates(*self._window_totals()),
            )
        self._notify()
        return snapshot

    def reset(self) -> None:
        """Force the breaker back to closed and zero every counter."""
        if self._try("reset") is None:
            self._local_reset()

    def _local_reset(self) -> None:
        with self._lock:
            self._failure_times.clear()
            self._slots.clear()
            if self._state is not CircuitState.CLOSED:
                self._log.info("circuit %s reset to closed", self.name)
                self._set_state(CircuitState.CLOSED)
            self._failures = 0
            self._trials = 0
            self._trial_successes = 0
            self._total_failures = 0
            self._total_successes = 0
            self._total_rejected = 0
            self._generation += 1
        self._notify()

    def force_open(self) -> None:
        """Force open for maintenance/tests, without simulating dependency failures."""
        if self._try("force_open") is None:
            self._local_force_open()

    def _local_force_open(self) -> None:
        with self._lock:
            self._open()
        self._notify()

    def _try(
        self, operation: str, generation: int = 0, trial: bool = False, slow: bool = False
    ) -> dict[str, Any] | None:
        """Backend result, or None when this call must use the local state (or be bypassed)."""
        if self._monitor is None:
            return None
        if self._async_backend:
            if operation == "state":
                return self._last
            raise TypeError(
                f"circuit {self.name} has an async backend; use the async form of this call"
            )
        return self._monitor.call(
            operation, lambda: self._remote(operation, generation, trial, slow), lambda: None
        )

    async def _atry(
        self, operation: str, generation: int = 0, trial: bool = False, slow: bool = False
    ) -> dict[str, Any] | None:
        """`_try` for an async backend: the store round trip is awaited."""
        if self._monitor is None:
            return None

        async def run() -> dict[str, Any]:
            extra = {"slow": True} if slow else {}  # backends without slow-call support still work
            data = await self.backend.operate(self, operation, generation, trial, **extra)
            return self._observe(data)

        return await self._monitor.acall(operation, run, lambda: None)

    async def areset(self) -> None:
        """`reset` for a breaker with an async backend (works for any breaker)."""
        if not self._async_backend:
            return self.reset()
        if await self._atry("reset") is None:
            self._local_reset()

    async def aforce_open(self) -> None:
        """`force_open` for a breaker with an async backend (works for any breaker)."""
        if not self._async_backend:
            return self.force_open()
        if await self._atry("force_open") is None:
            self._local_force_open()

    def _bypass(self) -> bool:
        return self._monitor is not None and self._monitor.mode == "allow"

    def _remote(
        self, operation: str, generation: int = 0, trial: bool = False, slow: bool = False
    ) -> dict[str, Any]:
        extra = {"slow": True} if slow else {}  # backends without slow-call support still work
        return self._observe(self.backend.operate(self, operation, generation, trial, **extra))

    def _observe(self, data: dict[str, Any]) -> dict[str, Any]:
        """Record a backend answer and report the transition it made, if any."""
        self._last = data
        if data["old"] != data["state"]:
            rates = self._rates(
                data.get("window_calls", 0),
                data.get("window_failures", 0),
                data.get("window_slow", 0),
            )
            _core.emit(
                "circuit.state_changed", self.name, old=data["old"], new=data["state"], **rates
            )
            if self.on_state_change:
                try:
                    self.on_state_change(
                        self, CircuitState(data["old"]), CircuitState(data["state"])
                    )
                except Exception:
                    self._log.exception("circuit listener failed")
        return data

    # -- failure and slow-call rates ------------------------------------------------
    def _rates(self, calls: int, failures: int, slow: int) -> dict[str, float | None]:
        if not self._rated:
            return {}
        return {
            "failure_rate": failures / calls if calls else 0.0,
            "slow_rate": slow / calls if calls else 0.0,
        }

    def _trips(self, calls: int, failures: int, slow: int) -> bool:
        if calls < self.minimum_calls:
            return False
        if self.failure_rate is not None and failures >= self.failure_rate * calls:
            return True
        return self.slow_call_rate is not None and slow >= self.slow_call_rate * calls

    def _slow(self, started: float | None) -> bool:
        return started is not None and _core.monotonic() - started >= self.slow_call_duration  # type: ignore[operator]

    # -- internals (call with the lock held) ---------------------------------------
    def _window_totals(
        self, failed: int = 0, slow: int = 0, add: bool = False
    ) -> tuple[int, int, int]:
        """Calls, failures and slow calls in ``window``, in ten slots; ``add`` records one."""
        if not self._rated:
            return 0, 0, 0
        slot = math.floor(_core.monotonic() / (self.window / 10))  # type: ignore[operator]
        while self._slots and self._slots[0][0] <= slot - 10:
            self._slots.popleft()
        if add:
            if not self._slots or self._slots[-1][0] != slot:
                self._slots.append([slot, 0, 0, 0])
            last = self._slots[-1]
            last[1], last[2], last[3] = last[1] + 1, last[2] + failed, last[3] + slow
        return (
            sum(s[1] for s in self._slots),
            sum(s[2] for s in self._slots),
            sum(s[3] for s in self._slots),
        )

    def _set_state(self, new: CircuitState) -> None:
        old = self._state
        self._state = new
        if old is not new:
            self._pending.append((old, new))

    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and _core.monotonic() - self._opened_at >= self.recovery_timeout
        ):
            self._set_state(CircuitState.HALF_OPEN)
            self._trials = 0
            self._trial_successes = 0
            self._generation += 1
            self._log.info("circuit %s is half-open; allowing trial calls", self.name)

    def _open(self) -> None:
        self._set_state(CircuitState.OPEN)
        self._opened_at = _core.monotonic()
        self._trials = 0
        self._trial_successes = 0
        self._generation += 1
        self._log.warning(
            "circuit %s opened after %d failure(s); rejecting calls for %.1fs",
            self.name,
            self._failures,
            self.recovery_timeout,
        )

    # -- internals (call WITHOUT the lock) -----------------------------------------
    def _notify(self) -> None:
        """Deliver pending transitions to the listener and the event hook."""
        if not self._pending:
            return
        with self._lock:
            pending, self._pending = self._pending, []
            rates = self._rates(*self._window_totals())
        for old, new in pending:
            _core.emit("circuit.state_changed", self.name, old=old, new=new, **rates)
            listener = self.on_state_change
            if listener is None:
                continue
            try:
                listener(self, old, new)
            except Exception:
                self._log.exception("circuit %s on_state_change listener failed", self.name)

    def _acquire(self) -> tuple[int, bool]:
        """Admit a call. Returns ``(generation, is_trial)`` or raises CircuitOpenError."""
        return self._admit(self._try("admit"))

    async def _aacquire(self) -> tuple[int, bool]:
        return self._admit(await self._atry("admit"))

    def _admit(self, data: dict[str, Any] | None) -> tuple[int, bool]:
        if data is None and self._bypass():
            return 0, False
        if data is not None:
            if not data["admitted"]:
                wait = data["wait"] / 1000
                _core.emit("circuit.rejected", self.name, retry_after=wait)
                raise CircuitOpenError(f"circuit {self.name} is {data['state']}", retry_after=wait)
            return data["generation"], bool(data["trial"])
        error: CircuitOpenError | None = None
        with self._lock:
            self._maybe_half_open()
            admitted = (self._generation, self._state is CircuitState.HALF_OPEN)
            if self._state is CircuitState.OPEN:
                remaining = max(self.recovery_timeout - (_core.monotonic() - self._opened_at), 0)
                error = CircuitOpenError(
                    f"circuit {self.name} is open; retry in {remaining:.1f}s",
                    retry_after=remaining,
                )
            elif self._state is CircuitState.HALF_OPEN:
                if self._trials >= self.half_open_max_calls:
                    error = CircuitOpenError(
                        f"circuit {self.name} is half-open and its trial calls are in use",
                        retry_after=0.0,
                    )
                else:
                    self._trials += 1
            if error is not None:
                self._total_rejected += 1
        self._notify()
        if error is not None:
            _core.emit("circuit.rejected", self.name, retry_after=error.retry_after)
            raise error
        return admitted

    def _success(self, generation: int, trial: bool, slow: bool = False) -> None:
        if self._try("success", generation, trial, slow) is None and not self._bypass():
            self._local_success(generation, trial, slow)

    async def _asuccess(self, generation: int, trial: bool, slow: bool = False) -> None:
        if await self._atry("success", generation, trial, slow) is None and not self._bypass():
            self._local_success(generation, trial, slow)

    def _local_success(self, generation: int, trial: bool, slow: bool = False) -> None:
        with self._lock:
            self._total_successes += 1
            if generation != self._generation:
                return
            if trial and slow and self.slow_call_rate is not None:
                self._trials = max(self._trials - 1, 0)
                self._open()
            elif trial:
                self._trials = max(self._trials - 1, 0)
                self._trial_successes += 1
                if self._trial_successes >= self.success_threshold:
                    self._slots.clear()
                    self._set_state(CircuitState.CLOSED)
                    self._trials = 0
                    self._trial_successes = 0
                    self._generation += 1
                    self._log.info(
                        "circuit %s closed after %d successful trial call(s)",
                        self.name,
                        self.success_threshold,
                    )
            if self.window is None or trial:
                self._failures = 0
                self._failure_times.clear()
            if self._rated and not trial and self._trips(*self._window_totals(0, slow, True)):
                self._open()
        self._notify()

    def _failure(self, generation: int, trial: bool, slow: bool = False) -> None:
        if self._try("failure", generation, trial, slow) is None and not self._bypass():
            self._local_failure(generation, trial, slow)

    def _local_failure(self, generation: int, trial: bool, slow: bool = False) -> None:
        with self._lock:
            self._total_failures += 1
            if generation != self._generation:
                return
            rated = self._rated and not trial and self._trips(*self._window_totals(1, slow, True))
            if self.failure_rate is not None:
                self._failures = self._window_totals()[1]
            elif self.window is None:
                self._failures += 1
            else:
                now = _core.monotonic()
                while self._failure_times and self._failure_times[0] <= now - self.window:
                    self._failure_times.popleft()
                self._failure_times.append(now)
                self._failures = len(self._failure_times)
            counted = self.failure_rate is None and self._failures >= self.failure_threshold
            if trial or rated or counted:
                self._open()
        self._notify()

    def _release(self, generation: int, trial: bool) -> None:
        """A non-counted exception ended the call: free its trial slot, if any."""
        if self._try("neutral", generation, trial) is None and not self._bypass():
            self._local_release(generation, trial)

    async def _arelease(self, generation: int, trial: bool) -> None:
        if await self._atry("neutral", generation, trial) is None and not self._bypass():
            self._local_release(generation, trial)

    def _local_release(self, generation: int, trial: bool) -> None:
        if not trial:
            return
        with self._lock:
            if generation == self._generation:
                self._trials = max(self._trials - 1, 0)

    def _counted(self, exc: BaseException) -> bool:
        return isinstance(exc, self.on) and not isinstance(exc, self.exclude)

    def _settle(self, generation: int, trial: bool, exc: BaseException, slow: bool = False) -> None:
        """Book a call that raised ``exc``: a failure, or uncounted."""
        if self._counted(exc):
            self._failure(generation, trial, slow)
        else:
            self._release(generation, trial)

    async def _asettle(
        self, generation: int, trial: bool, exc: BaseException, slow: bool = False
    ) -> None:
        if not self._counted(exc):
            return await self._arelease(generation, trial)
        if await self._atry("failure", generation, trial, slow) is None and not self._bypass():
            self._local_failure(generation, trial, slow)

    def _offload(self, name: str, *args: Any) -> asyncio.Task[Any]:
        """Run a backend step as a task: awaited natively, or in a worker thread."""
        if self._async_backend:
            return asyncio.ensure_future(getattr(self, "_a" + name.lstrip("_"))(*args))
        return asyncio.create_task(asyncio.to_thread(getattr(self, name), *args))

    # -- public call API -----------------------------------------------------------
    @overload
    def call(
        self, func: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...
    @overload
    def call(self, func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R: ...
    def call(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call ``func(*args, **kwargs)`` through the breaker.

        For an ``async def`` function this returns a coroutine to await.

        Args:
            func: The function to call.
            *args: Positional arguments for ``func``.
            **kwargs: Keyword arguments for ``func``.

        Returns:
            Whatever ``func`` returns (a coroutine if it is async).

        Raises:
            CircuitOpenError: If the circuit is open (``func`` is not called).
        """
        if _core.is_async(func):
            return self._acall(func, args, kwargs)
        return self._call_sync(func, args, kwargs)

    def _call_sync(self, func: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        if self._async_backend:
            raise TypeError(f"circuit {self.name} has an async backend; it protects async def only")
        generation, trial = self._acquire()
        started = None if self.slow_call_duration is None else _core.monotonic()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            try:
                self._settle(generation, trial, exc, self._slow(started))
            except Exception:
                self._log.exception("circuit outcome bookkeeping failed; preserving original error")
            raise
        self._success(generation, trial, self._slow(started))
        return result

    async def _acall(self, func: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        from python_wrap.coordination import _drain

        # Synchronous network admission cannot be abandoned on cancellation: a late
        # successful admission would otherwise strand a half-open trial reservation.
        if self.backend is not None:
            admission = self._offload("_acquire")
            try:
                generation, trial = await asyncio.shield(admission)
            except asyncio.CancelledError:
                acquired, error, _ = await _drain(admission)
                if error is None:
                    cleanup = self._offload("_release", *acquired)
                    await _drain(cleanup)
                raise
        else:
            generation, trial = self._acquire()
        started = None if self.slow_call_duration is None else _core.monotonic()
        try:
            result = await func(*args, **kwargs)
        except BaseException as exc:
            slow = self._slow(started)
            if self.backend is not None:
                settlement = self._offload("_settle", generation, trial, exc, slow)
                _, error, cancelled = await _drain(settlement)
                if cancelled:
                    raise asyncio.CancelledError from None
                if error is not None:
                    self._log.error(
                        "circuit outcome bookkeeping failed; preserving original error",
                        exc_info=(type(error), error, error.__traceback__),
                    )
            else:
                try:
                    self._settle(generation, trial, exc, slow)
                except Exception:
                    self._log.exception(
                        "circuit outcome bookkeeping failed; preserving original error"
                    )
            raise
        slow = self._slow(started)
        if self.backend is not None:
            success = self._offload("_success", generation, trial, slow)
            _, error, cancelled = await _drain(success)
            if cancelled:
                raise asyncio.CancelledError
            if error is not None:
                raise error
        else:
            self._success(generation, trial, slow)
        return result

    def __call__(self, func: F) -> F:
        """Decorate ``func`` so every call goes through this breaker.

        The wrapper keeps the exact type of ``func``, so attributes added by decorators
        applied underneath (such as ``cache_info()`` from ``memoize``) stay visible.
        """
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self._acall(func, args, kwargs)

            async_wrapper.__python_wrap_layer__ = ("breaker", self)  # type: ignore[attr-defined]
            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Sync vs async was decided above, once: no per-call detection.
            return self._call_sync(func, args, kwargs)

        wrapper.__python_wrap_layer__ = ("breaker", self)  # type: ignore[attr-defined]
        return cast(F, wrapper)

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name!r}, state={self.state.value!r})"


class CircuitProtected(Protocol[P, R_co]):
    """A function wrapped by `circuit_breaker`: callable, with its own breaker attached.

    Use it to annotate a decorated function, e.g. ``CircuitProtected[[int], str]``.
    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: CircuitProtected[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _CircuitProtectedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> CircuitProtected[P, R_co]: ...
    @overload
    def __get__(
        self: CircuitProtected[Concatenate[Any, P2], R_co],
        instance: object,
        owner: type | None = None,
    ) -> CircuitProtected[P2, R_co]: ...

    @property
    def breaker(self) -> CircuitBreaker:
        """The dedicated `CircuitBreaker` (inspect ``.state`` or call ``.reset()``)."""
        ...


class _CircuitProtectedOnClass(CircuitProtected[P, R_co], Protocol[P, P2, R_co]):
    """`CircuitProtected` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


@overload
def circuit_breaker(
    func: Callable[P, R],
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    success_threshold: int = ...,
    on: ExceptionTypes = ...,
    exclude: ExceptionTypes = ...,
    on_state_change: StateChangeListener | None = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
    window: float | None = ...,
    failure_rate: float | None = ...,
    minimum_calls: int = ...,
    slow_call_duration: float | None = ...,
    slow_call_rate: float | None = ...,
) -> CircuitProtected[P, R]: ...
@overload
def circuit_breaker(
    func: None = None,
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    success_threshold: int = ...,
    on: ExceptionTypes = ...,
    exclude: ExceptionTypes = ...,
    on_state_change: StateChangeListener | None = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
    window: float | None = ...,
    failure_rate: float | None = ...,
    minimum_calls: int = ...,
    slow_call_duration: float | None = ...,
    slow_call_rate: float | None = ...,
) -> Callable[[Callable[P, R]], CircuitProtected[P, R]]: ...
def circuit_breaker(
    func: Callable[..., Any] | None = None,
    /,
    *,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
    half_open_max_calls: int = 1,
    success_threshold: int = 1,
    on: ExceptionTypes = Exception,
    exclude: ExceptionTypes = (),
    on_state_change: StateChangeListener | None = None,
    name: str | None = None,
    logger: LoggerLike = None,
    window: float | None = None,
    failure_rate: float | None = None,
    minimum_calls: int = 10,
    slow_call_duration: float | None = None,
    slow_call_rate: float | None = None,
) -> Any:
    """Decorate a function with its own dedicated `CircuitBreaker`.

    The wrapper exposes the breaker as ``wrapper.breaker``. To share one breaker
    between several functions, create a `CircuitBreaker` and use it as a
    decorator instead.

    Args:
        func: The function to wrap (when used without parentheses).
        failure_threshold: Consecutive failures that open the circuit.
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Trial calls allowed while half-open.
        success_threshold: Consecutive successful trials needed to close again.
        on: Exception type(s) counted as failures.
        exclude: Exception type(s) never counted, even when they match ``on``.
        on_state_change: Listener called with ``(breaker, old_state, new_state)``.
        name: Name for log messages, errors and events (defaults to the function's qualified name).
        logger: Logger (or name) for state transitions.
        window: Count failures inside this many seconds (see `CircuitBreaker`).
        failure_rate: Open at this failed share of calls in ``window``.
        minimum_calls: Calls in ``window`` needed before a rate can open the breaker.
        slow_call_duration: Seconds after which a call counts as slow.
        slow_call_rate: Open at this slow share of calls in ``window``.

    Returns:
        The wrapped function, or a decorator when ``func`` is omitted.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        >>> @circuit_breaker(failure_threshold=3, recovery_timeout=10)
        ... def call_api() -> None: ...
    """

    def decorate(f: Callable[..., Any]) -> Any:
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
            on=on,
            exclude=exclude,
            on_state_change=on_state_change,
            name=name or _core.qualname(f),
            logger=logger,
            window=window,
            failure_rate=failure_rate,
            minimum_calls=minimum_calls,
            slow_call_duration=slow_call_duration,
            slow_call_rate=slow_call_rate,
        )
        wrapped: Any = breaker(f)
        wrapped.breaker = breaker
        return wrapped

    return decorate if func is None else decorate(func)
