"""One failure rule for every shared backend: ``on_backend_error``.

``"raise"`` propagates the store error, ``"local"`` falls back to the in-process
implementation and ``"allow"`` admits the call and skips accounting. After an error the
store is skipped for ``retry_backend_after`` seconds so a dead store does not add its
connect timeout to every call. Emits ``backend.error`` and ``backend.recovered`` and
reports ``degraded`` through `python_wrap.health.health` while a fallback is active.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from python_wrap import _core, _fork

__all__ = ["BACKEND_ERROR_MODES", "BackendMonitor"]

T = TypeVar("T")
BACKEND_ERROR_MODES = ("raise", "local", "allow")


def default_mode() -> str:
    from python_wrap.config import get_config

    return str(get_config()["backend_errors"] or "raise")


class BackendMonitor:
    """Tracks one shared backend's health and applies its ``on_backend_error`` mode."""

    def __init__(
        self,
        kind: str,
        name: str,
        on_backend_error: str | None = None,
        retry_backend_after: float = 1.0,
        *,
        register: bool = True,
    ) -> None:
        if on_backend_error is not None and on_backend_error not in BACKEND_ERROR_MODES:
            raise ValueError(f"on_backend_error must be one of {BACKEND_ERROR_MODES}")
        if retry_backend_after < 0:
            raise ValueError("retry_backend_after must be >= 0")
        self.kind, self.name = kind, name
        self._mode = on_backend_error
        self.retry_backend_after = retry_backend_after
        self._until = 0.0
        self._down = False
        self._lock = _fork.ForkSafeLock()
        _fork.register(self)
        if register:
            from python_wrap.health import health

            health.register(f"backend:{kind}:{name}", self)

    def _reset_after_fork(self) -> None:
        self._until, self._down = 0.0, False

    @property
    def mode(self) -> str:
        return self._mode or default_mode()

    @property
    def degraded(self) -> bool:
        return self._down

    def _skipping(self) -> bool:
        return self.mode != "raise" and time.monotonic() < self._until

    def failed(self, exc: BaseException, operation: str) -> None:
        with self._lock:
            self._down = True
            self._until = time.monotonic() + self.retry_backend_after
        _core.get_logger(None).warning(
            "%s backend %r: %s failed (%s); on_backend_error=%s",
            self.kind,
            self.name,
            operation,
            type(exc).__name__,
            self.mode,
        )
        _core.emit(
            "backend.error",
            self.name,
            backend=self.kind,
            operation=operation,
            error=type(exc).__name__,
        )

    def succeeded(self) -> None:
        if self._down:
            with self._lock:
                recovered, self._down = self._down, False
            if recovered:
                _core.emit("backend.recovered", self.name, backend=self.kind)

    def call(self, operation: str, fn: Callable[[], T], fallback: Callable[[], T]) -> T:
        """Run ``fn``; on a store error apply the mode (``fallback`` serves local/allow)."""
        if self._skipping():
            return fallback()
        try:
            result = fn()
        except Exception as exc:
            self.failed(exc, operation)
            if self.mode == "raise":
                raise
            return fallback()
        self.succeeded()
        return result

    async def acall(
        self, operation: str, fn: Callable[[], Awaitable[T]], fallback: Callable[[], Any]
    ) -> T:
        """`call` for awaitable store operations; ``fallback`` may be sync or async."""
        import inspect

        async def fall() -> Any:
            value = fallback()
            return await value if inspect.isawaitable(value) else value

        if self._skipping():
            return await fall()  # type: ignore[no-any-return]
        try:
            result = await fn()
        except Exception as exc:
            self.failed(exc, operation)
            if self.mode == "raise":
                raise
            return await fall()  # type: ignore[no-any-return]
        self.succeeded()
        return result
