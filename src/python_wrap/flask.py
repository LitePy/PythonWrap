"""Flask request context adapter; Flask is imported only by init_app()."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python_wrap._integration import request_id, scope
from python_wrap._runtime import seconds
from python_wrap.health import health

__all__ = ["init_app"]


def init_app(
    app: Any,
    *,
    user: Callable[[], Any] | None = None,
    request_id_header: str = "X-Request-ID",
    budget: float | None = None,
    health_path: str | None = None,
) -> None:
    """Bind request/user context and an optional cooperative deadline.

    Health routes are opt-in; apply your framework's authorization policy before
    exposing dependency details. Install ContextFilter on app handlers separately.
    Repeated initialization on the same app is a no-op.
    """
    import importlib

    flask = importlib.import_module("flask")
    request = flask.request

    if app.extensions.get("python_wrap"):
        return
    budget = None if budget is None else seconds(budget, positive=False)

    def before() -> None:
        fields: dict[str, Any] = {"request_id": request_id(request.headers.get(request_id_header))}
        if user:
            fields["user"] = user()
        stack = scope(fields, budget)
        request.environ["python_wrap.scope"] = stack

    def after(error: Any = None) -> None:
        stack = request.environ.pop("python_wrap.scope", None)
        if stack is not None:
            stack.close()

    app.before_request(before)
    app.teardown_request(after)
    if health_path:
        app.add_url_rule(health_path, "python_wrap_health", health.report, methods=["GET"])

    app.extensions["python_wrap"] = True
