"""An explicit, weakly held registry of dependency state and optional health checks."""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable
from typing import Any

from python_wrap import _fork

__all__ = ["HealthRegistry", "health"]


class HealthRegistry:
    """Register named components; report structural state without calling user functions.

    Backend checks are opt-in through register_check. Reports omit exception messages,
    endpoints and credentials. Applications choose whether to expose a health route.
    """

    def __init__(self) -> None:
        self._lock = _fork.ForkSafeLock()
        self._objects: dict[str, weakref.ref[Any]] = {}
        self._checks: dict[str, Callable[[], Any]] = {}

    def register(self, name: str, component: Any) -> None:
        if not name:
            raise ValueError("health name must not be empty")
        ref = weakref.ref(component)
        with self._lock:
            self._objects[name] = ref
            self._checks.pop(name, None)

    def register_check(self, name: str, check: Callable[[], Any]) -> None:
        """Register a synchronous checker returning bool or a dict with a healthy bool."""
        if not name or inspect.iscoroutinefunction(check):
            raise ValueError("health checks need a name and a synchronous checker")
        with self._lock:
            self._checks[name] = check
            self._objects.pop(name, None)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._objects.pop(name, None)
            self._checks.pop(name, None)

    def report(self) -> dict[str, Any]:
        with self._lock:
            objects = list(self._objects.items())
            checks = list(self._checks.items())
        result: dict[str, Any] = {}
        for name, ref in objects:
            obj = ref()
            if obj is None:
                continue
            try:
                state = getattr(obj, "state", None)
                if state is not None:
                    value = getattr(state, "value", state)
                    result[name] = {"healthy": value == "closed", "state": value}
                elif hasattr(obj, "active") and hasattr(obj, "waiting"):
                    result[name] = {"healthy": True, "active": obj.active, "waiting": obj.waiting}
                elif hasattr(obj, "cache_info"):
                    info = obj.cache_info()
                    result[name] = {
                        "healthy": True,
                        "hits": info.hits,
                        "misses": info.misses,
                        "size": info.currsize,
                    }
                else:
                    result[name] = {"healthy": True, "status": "registered", "checked": False}
            except Exception as exc:
                result[name] = {"healthy": False, "error": type(exc).__name__}
        for name, check in checks:
            try:
                value = check()
                if inspect.iscoroutine(value):
                    value.close()
                    raise TypeError("health check returned a coroutine")
                if isinstance(value, dict) and isinstance(value.get("healthy"), bool):
                    result[name] = value
                elif isinstance(value, bool):
                    result[name] = {"healthy": value}
                else:
                    raise TypeError("health checks must return bool or a dict with healthy: bool")
            except Exception as exc:
                result[name] = {"healthy": False, "error": type(exc).__name__}
        return result


health = HealthRegistry()
