"""Pure ASGI context middleware for FastAPI/Starlette, with no framework imports."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python_wrap._integration import request_id, scope
from python_wrap._runtime import seconds
from python_wrap.health import health

__all__ = ["ContextMiddleware", "install"]


class ContextMiddleware:
    """Bind context around HTTP dispatch/streaming and restore on errors/cancellation."""

    def __init__(
        self,
        app: Any,
        *,
        user: Callable[[dict[str, Any]], Any] | None = None,
        request_id_header: str = "X-Request-ID",
        budget: float | None = None,
    ) -> None:
        budget = None if budget is None else seconds(budget, positive=False)
        self.app, self.user, self.budget = app, user, budget
        self.header = request_id_header.lower().encode("latin1")

    async def __call__(self, asgi_scope: dict[str, Any], receive: Any, send: Any) -> None:
        if asgi_scope["type"] != "http":
            await self.app(asgi_scope, receive, send)
            return
        import inspect

        headers = dict(asgi_scope.get("headers", []))
        raw = headers.get(self.header, b"")
        fields = {"request_id": request_id(raw.decode("latin1"))}
        if self.user:
            value = self.user(asgi_scope)
            fields["user"] = await value if inspect.isawaitable(value) else value
        with scope(fields, self.budget):
            await self.app(asgi_scope, receive, send)


def install(
    app: Any,
    *,
    user: Callable[[dict[str, Any]], Any] | None = None,
    request_id_header: str = "X-Request-ID",
    budget: float | None = None,
    health_path: str | None = None,
) -> None:
    """Install middleware before app startup; optional health endpoint returns JSON."""
    if getattr(app.state, "_python_wrap_installed", False):
        return
    app.add_middleware(
        ContextMiddleware, user=user, request_id_header=request_id_header, budget=budget
    )
    if health_path:
        app.get(health_path)(health.report)
    app.state._python_wrap_installed = True
