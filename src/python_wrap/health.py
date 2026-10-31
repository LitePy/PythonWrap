"""An explicit, weakly held registry of dependency state and optional health checks."""

from __future__ import annotations

import inspect
import json
import weakref
from collections.abc import Callable, Iterable
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

    def summary(self, *, critical: Iterable[str] | None = None) -> dict[str, Any]:
        """An overall status for load balancers and dashboards.

        ``status`` is ``"ok"`` when every component is healthy, ``"down"`` when a
        *critical* component is unhealthy, otherwise ``"degraded"``. With
        ``critical=None`` every component is critical. ``http_status`` is 200 for
        ``ok``/``degraded`` and 503 for ``down`` (so an optional dependency with an
        open breaker does not take the instance out of rotation).

        Args:
            critical: Component names whose failure means the service is down.

        Returns:
            ``{"status", "http_status", "healthy", "unhealthy", "components"}``.

        Example:
            >>> registry = HealthRegistry()
            >>> registry.register_check("db", lambda: True)
            >>> registry.register_check("cache", lambda: False)
            >>> registry.summary(critical=["db"])["status"]
            'degraded'
        """
        components = self.report()
        unhealthy = sorted(name for name, data in components.items() if not data["healthy"])
        required = set(components) if critical is None else set(critical)
        if not unhealthy:
            status = "ok"
        elif required & set(unhealthy):
            status = "down"
        else:
            status = "degraded"
        return {
            "status": status,
            "http_status": 503 if status == "down" else 200,
            "healthy": len(components) - len(unhealthy),
            "unhealthy": unhealthy,
            "components": components,
        }

    def _response(self, critical: Iterable[str] | None) -> tuple[int, bytes]:
        summary = self.summary(critical=critical)
        return summary["http_status"], json.dumps(summary, default=str).encode()

    def wsgi_app(self, *, critical: Iterable[str] | None = None) -> Callable[..., Any]:
        """A WSGI app answering every request with `summary` as JSON (200 or 503).

        Mount it at ``/health`` (Flask: ``DispatcherMiddleware``; Django: call it from a
        view). Do not expose it publicly if component names are sensitive.
        """
        names = None if critical is None else tuple(critical)

        def app(environ: Any, start_response: Callable[..., Any]) -> list[bytes]:
            status, body = self._response(names)
            start_response(
                f"{status} {'OK' if status == 200 else 'Service Unavailable'}",
                [("Content-Type", "application/json"), ("Cache-Control", "no-store")],
            )
            return [body]

        return app

    def asgi_app(self, *, critical: Iterable[str] | None = None) -> Callable[..., Any]:
        """An ASGI app answering HTTP requests with `summary` as JSON (200 or 503).

        Mount it in FastAPI/Starlette with ``app.mount("/health", health.asgi_app())``.
        Checks are synchronous and run in a worker thread, so slow checks never block
        the event loop.
        """
        import asyncio

        names = None if critical is None else tuple(critical)

        async def app(scope: Any, receive: Any, send: Any) -> None:
            if scope.get("type") != "http":
                return
            status, body = await asyncio.to_thread(self._response, names)
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        return app


health = HealthRegistry()
