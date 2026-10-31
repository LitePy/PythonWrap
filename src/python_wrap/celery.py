"""Celery integration: task context, Celery-aware retry countdowns and shared breakers.

Imported only when used; Celery itself is never imported at module level.

Two layers of retry exist in a Celery app: python-wrap's in-process `retry` (fast, a
few seconds, inside one task run) and Celery's own task retry (re-queued, minutes,
survives worker restarts). `retry_countdown` and `retry_task` make the second layer
follow the same schedule rules (explicit delays, exponential backoff with a cap, and
the dependency's own ``retry_after`` from `CircuitOpenError`, `RateLimitExceeded` or a
``Retry-After`` header). `shared_breaker` builds a breaker whose state is shared by all
prefork children, which otherwise each keep their own.
"""

from __future__ import annotations

import contextvars
import random
from collections.abc import Sequence
from typing import Any, NoReturn

from python_wrap import _fork
from python_wrap._integration import scope

__all__ = ["TaskContext", "install", "retry_countdown", "retry_task", "shared_breaker"]


class TaskContext:
    """Signal subscription handle; close disconnects only this app's handlers."""

    def __init__(self, app: Any, *, use_soft_deadline: bool = True) -> None:
        import importlib

        signals = importlib.import_module("celery.signals")
        task_postrun, task_prerun = signals.task_postrun, signals.task_prerun

        self.app, self.use_soft_deadline = app, use_soft_deadline
        self.closed = False
        self._active = 0
        self._lock = _fork.ForkSafeLock()
        self._scopes: contextvars.ContextVar[tuple[Any, ...]] = contextvars.ContextVar(
            "python_wrap_task_scopes", default=()
        )
        task_prerun.connect(self._before, weak=False)
        task_postrun.connect(self._after, weak=False)
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._scopes.set(())
        self._active = 0

    def _before(
        self, sender: Any = None, task_id: str | None = None, task: Any = None, **kw: Any
    ) -> None:
        task = task if task is not None else sender
        if getattr(task, "app", None) is not self.app:
            return
        with self._lock:
            if self.closed:
                return
            self._active += 1
        budget = getattr(task, "soft_time_limit", None) if self.use_soft_deadline else None
        limits = getattr(getattr(task, "request", None), "timelimit", None)
        if self.use_soft_deadline and limits and len(limits) > 1 and limits[1] is not None:
            budget = limits[1]
        try:
            stack = scope({"task_id": task_id, "task_name": getattr(task, "name", None)}, budget)
        except BaseException:
            with self._lock:
                self._active -= 1
                disconnect = self.closed and self._active == 0
            if disconnect:
                self._disconnect_after()
            raise
        self._scopes.set((*self._scopes.get(), (task_id, stack)))

    def _after(
        self, sender: Any = None, task: Any = None, task_id: str | None = None, **kw: Any
    ) -> None:
        task = task if task is not None else sender
        if getattr(task, "app", None) is not self.app:
            return
        stacks = self._scopes.get()
        if stacks and stacks[-1][0] == task_id:
            self._scopes.set(stacks[:-1])
            try:
                stacks[-1][1].close()
            finally:
                with self._lock:
                    self._active -= 1
                    disconnect = self.closed and self._active == 0
                if disconnect:
                    self._disconnect_after()

    def _disconnect_after(self) -> None:
        import importlib

        importlib.import_module("celery.signals").task_postrun.disconnect(self._after)

    def close(self) -> None:
        """Refuse future tasks; active task contexts still restore at postrun."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            disconnect = self._active == 0
        import importlib

        importlib.import_module("celery.signals").task_prerun.disconnect(self._before)
        if disconnect:
            self._disconnect_after()


def install(app: Any, *, use_soft_deadline: bool = True) -> TaskContext:
    """Bind task_id/name and an optional soft-time-limit deadline around task execution.

    Fork-safe python-wrap components reset through their POSIX hooks automatically;
    this adapter never starts background loops or opens backend connections at import.
    """
    existing = getattr(app, "_python_wrap_context", None)
    if existing is not None and not existing.closed:
        return existing  # type: ignore[no-any-return]
    handle = TaskContext(app, use_soft_deadline=use_soft_deadline)
    app._python_wrap_context = handle
    return handle


def retry_countdown(
    task: Any,
    exc: BaseException | None = None,
    *,
    delays: Sequence[float] | None = None,
    delay: float = 5.0,
    backoff: float = 2.0,
    max_delay: float = 600.0,
    jitter: float = 0.0,
    respect_retry_after: bool = True,
) -> float:
    """Seconds to wait before Celery runs ``task`` again, following python-wrap rules.

    The retry number comes from ``task.request.retries`` (0 on the first failure). A
    wait requested by the failure itself wins: ``exc.retry_after`` (set by
    `CircuitOpenError` and `RateLimitExceeded`) or a ``Retry-After`` header on
    ``exc.response``, capped at ``max_delay``. Otherwise entry ``n`` of ``delays``
    (repeating the last one), or ``delay * backoff ** n`` capped at ``max_delay``, plus
    up to ``jitter`` random seconds.

    Args:
        task: A bound Celery task (``bind=True``) or anything with ``request.retries``.
        exc: The exception that caused the retry.
        delays: Explicit schedule, e.g. ``[10, 60, 300]``.
        delay: First wait for the exponential schedule.
        backoff: Multiplier per retry (>= 1).
        max_delay: Upper bound for every wait.
        jitter: Random extra seconds added to scheduled (not server-requested) waits.
        respect_retry_after: Honour a wait carried by the exception.

    Returns:
        Non-negative seconds, suitable for ``task.retry(countdown=...)``.

    Raises:
        ValueError: For a negative delay/jitter/max_delay, backoff below 1 or an empty
            ``delays``.

    Example:
        ```python
        from types import SimpleNamespace

        from python_wrap import CircuitOpenError
        from python_wrap.celery import retry_countdown

        task = SimpleNamespace(request=SimpleNamespace(retries=2))
        assert retry_countdown(task, delays=[10, 60, 300]) == 300
        assert retry_countdown(task, CircuitOpenError("open", retry_after=42)) == 42
        ```
    """
    from python_wrap.retry import retry_after_header

    if delay < 0 or max_delay < 0 or jitter < 0 or backoff < 1:
        raise ValueError("delay, max_delay and jitter must be >= 0 and backoff >= 1")
    if delays is not None and not delays:
        raise ValueError("delays must not be empty")
    if respect_retry_after and exc is not None:
        hinted = retry_after_header(exc)
        if hinted is not None:
            return min(hinted, max_delay)
    retries = int(getattr(getattr(task, "request", None), "retries", 0) or 0)
    if delays is not None:
        base = float(delays[min(retries, len(delays) - 1)])
    else:
        base = delay * backoff**retries
    return min(base, max_delay) + (random.uniform(0, jitter) if jitter else 0.0)


def retry_task(
    task: Any,
    exc: BaseException,
    *,
    max_retries: int | None = None,
    delays: Sequence[float] | None = None,
    delay: float = 5.0,
    backoff: float = 2.0,
    max_delay: float = 600.0,
    jitter: float = 0.0,
    respect_retry_after: bool = True,
) -> NoReturn:
    """Ask Celery to retry ``task`` later with a `retry_countdown` schedule.

    Raises whatever ``task.retry`` raises (Celery's ``Retry``, or ``exc`` itself once
    ``max_retries`` is exhausted). Use it inside ``except`` in a bound task, typically
    after an in-process `retry` gave up or a breaker is open::

        @app.task(bind=True, max_retries=5)
        def sync_orders(self, account_id):
            try:
                return fetch_orders(account_id)  # decorated with retry + breaker
            except (ConnectionError, CircuitOpenError) as exc:
                retry_task(self, exc, delays=[30, 120, 600])

    Args:
        task: The bound Celery task.
        exc: The failure; re-raised by Celery when retries are exhausted.
        max_retries: Overrides the task's ``max_retries`` when given.
        delays: Passed to `retry_countdown`, as are the remaining options.
        delay: See `retry_countdown`.
        backoff: See `retry_countdown`.
        max_delay: See `retry_countdown`.
        jitter: See `retry_countdown`.
        respect_retry_after: See `retry_countdown`.

    Raises:
        Exception: Always; this function never returns.
    """
    countdown = retry_countdown(
        task,
        exc,
        delays=delays,
        delay=delay,
        backoff=backoff,
        max_delay=max_delay,
        jitter=jitter,
        respect_retry_after=respect_retry_after,
    )
    options: dict[str, Any] = {"exc": exc, "countdown": countdown}
    if max_retries is not None:
        options["max_retries"] = max_retries
    raise task.retry(**options)


def shared_breaker(name: str, backend: Any, **options: Any) -> Any:
    """A `CircuitBreaker` whose state lives in ``backend``, shared by every worker process.

    Prefork Celery workers (and gunicorn/uvicorn workers) each hold their own in-process
    breaker state, so a dead vendor is probed by every child separately. Give one stable
    ``name`` per dependency and a shared backend: a Redis client (wrapped in
    `RedisBreakerState`), or any object with an ``operate`` method such as
    `RedisBreakerState` or a single-host SQLite state backend.

    Args:
        name: Stable breaker name, identical in every process (e.g. ``"vendor-a"``).
        backend: A Redis client, or a ready breaker state backend.
        **options: Other `CircuitBreaker` options (failure_threshold, on, ...).

    Returns:
        A `CircuitBreaker`.

    Example:
        ```python
        from python_wrap.celery import shared_breaker


        class StateBackend:  # stands in for RedisBreakerState in this example
            def operate(self, breaker, operation, generation=0, trial=False):
                return {"admitted": 1, "state": "closed", "generation": 0, "trial": 0,
                        "wait": 0, "failures": 0, "total_failures": 0,
                        "total_successes": 0, "total_rejected": 0, "opened": 0,
                        "old": "closed"}


        breaker = shared_breaker("vendor-a", StateBackend(), failure_threshold=3)
        assert breaker.name == "vendor-a"
        ```
    """
    from python_wrap.circuit import CircuitBreaker
    from python_wrap.shared import RedisBreakerState

    if not name:
        raise ValueError("shared breakers need a stable, nonempty name")
    state = backend if hasattr(backend, "operate") else RedisBreakerState(backend)
    return CircuitBreaker(name=name, backend=state, **options)
