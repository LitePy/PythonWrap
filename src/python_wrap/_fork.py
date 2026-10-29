"""Weakly registered child resets; no application work is started at fork time."""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Callable
from types import TracebackType
from typing import Any

__all__ = ["ForkSafeLock", "ForkSafeRLock", "register", "register_reset", "unregister"]
_registry: weakref.WeakSet[Any] = weakref.WeakSet()
_registry_lock = threading.Lock()
_installed = False


def register(obj: Any) -> None:
    """Weakly register an object's `_reset_after_fork` method on POSIX."""
    if not hasattr(os, "register_at_fork"):
        return
    global _installed
    with _registry_lock:
        if not _installed:
            os.register_at_fork(after_in_child=_after_fork)
            _installed = True
        _registry.add(obj)


def unregister(obj: Any) -> None:
    """Remove an object from the reset registry."""
    with _registry_lock:
        _registry.discard(obj)


def _after_fork() -> None:
    global _registry_lock
    _registry_lock = threading.Lock()
    objects = list(_registry)
    # Callbacks may acquire their component's lock: reset every lock first.
    for obj in objects:
        if isinstance(obj, (ForkSafeLock, ForkSafeRLock)):
            obj._reset_after_fork()
    for obj in objects:
        if not isinstance(obj, (ForkSafeLock, ForkSafeRLock)):
            obj._reset_after_fork()


class ForkSafeLock:
    """Lock with the acquire/release/context API, reset in a forked child."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        register(self)

    def _reset_after_fork(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


class ForkSafeRLock:
    """Reentrant counterpart of `ForkSafeLock`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        register(self)

    def _reset_after_fork(self) -> None:
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


class _Reset:
    def __init__(self, reset: Callable[[], None]) -> None:
        self.reset = reset
        register(self)

    def _reset_after_fork(self) -> None:
        self.reset()


def register_reset(reset: Callable[[], None]) -> _Reset:
    """Return a weakly registered handle; its owner must keep the handle alive."""
    return _Reset(reset)
