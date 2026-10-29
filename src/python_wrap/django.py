"""Sync/async Django middleware for request context and cooperative deadlines."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python_wrap import _core
from python_wrap._integration import request_id, scope
from python_wrap._runtime import seconds

__all__ = ["ContextMiddleware"]


class ContextMiddleware:
    """Add python_wrap.django.ContextMiddleware to Django's MIDDLEWARE.

    Optional constructor arguments are suitable for a configured middleware factory.
    Context covers view dispatch; streaming iterator code should bind its own context.
    The default user field uses the authenticated user's primary key, not credentials.
    """

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[..., Any],
        *,
        budget: float | None = None,
        user: Callable[[Any], Any] | None = None,
    ) -> None:
        budget = None if budget is None else seconds(budget, positive=False)
        self.get_response, self.budget, self.user = get_response, budget, user
        import importlib

        try:
            async_helpers = importlib.import_module("asgiref.sync")
        except ImportError:
            async_helpers = None
        self.is_async = _core.is_async(get_response) or (
            async_helpers is not None and async_helpers.iscoroutinefunction(get_response)
        )
        if self.is_async:
            if async_helpers is None:
                raise ImportError("Django async middleware requires asgiref")
            async_helpers.markcoroutinefunction(self)

    def _scope(self, request: Any) -> Any:
        fields: dict[str, Any] = {"request_id": request_id(request.META.get("HTTP_X_REQUEST_ID"))}
        if self.user:
            fields["user"] = self.user(request)
        else:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                fields["user"] = str(user.pk)
        return scope(fields, self.budget)

    def __call__(self, request: Any) -> Any:
        if self.is_async:
            return self._acall(request)
        with self._scope(request):
            return self.get_response(request)

    async def _acall(self, request: Any) -> Any:
        import importlib

        async_helpers = importlib.import_module("asgiref.sync")
        sync_to_async = async_helpers.sync_to_async
        fields: dict[str, Any] = {"request_id": request_id(request.META.get("HTTP_X_REQUEST_ID"))}
        if self.user:
            if _core.is_async(self.user) or async_helpers.iscoroutinefunction(self.user):
                fields["user"] = await self.user(request)
            else:
                fields["user"] = await sync_to_async(self.user, thread_sensitive=True)(request)
        elif hasattr(request, "auser"):
            user = await request.auser()
            if getattr(user, "is_authenticated", False):
                fields["user"] = str(user.pk)
        else:

            def user_value() -> str | None:
                user = getattr(request, "user", None)
                return str(user.pk) if user is not None and user.is_authenticated else None

            value = await sync_to_async(user_value, thread_sensitive=True)()
            if value is not None:
                fields["user"] = value
        with scope(fields, self.budget):
            return await self.get_response(request)
