"""Celery task-context integration, imported only when installed explicitly."""

from __future__ import annotations

import contextvars
from typing import Any

from python_wrap import _fork
from python_wrap._integration import scope

__all__ = ["TaskContext", "install"]


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
