"""HTTP-level fault injection for LLM clients: ``chaos.llm_http``.

`LLMHTTPFaults` reuses the rules of `python_wrap.llm_testing.LLMFaults` but delivers them
on the wire. Wrap any ``httpx`` transport (a real one, a ``MockTransport`` or a
`python_wrap.llm_cassette.ReplayTransport`) with ``chaos.llm_http.transport(inner)`` and
the provider SDK on top sees what a flaky API produces: real 429 and 529 responses with a
``Retry-After`` header, 5xx errors, a request that hangs, a read timeout, a body cut short
or a stream that stalls halfway. That exercises the SDK's own error mapping and your
retries together, which the in-process fakes cannot.

Like all chaos rules nothing happens until a rule is added.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from python_wrap import _core
from python_wrap.chaos import Fault, FaultInjector
from python_wrap.llm_cassette import (
    TransportContext,
    _header_pairs,
    _httpx,
    build_response,
    stream_class,
)
from python_wrap.llm_testing import APIStatusError, APITimeoutError, LLMFaults

__all__ = ["ChaosTransport", "LLMHTTPFaults"]

POINT = "llm.http"

_ERROR_TYPES = {
    429: "rate_limit_error",
    529: "overloaded_error",
}


@dataclasses.dataclass(frozen=True)
class _Truncate:
    keep: float


@dataclasses.dataclass(frozen=True)
class _StallStream:
    after_chunks: int
    seconds: float


class LLMHTTPFaults(LLMFaults):
    """LLM fault rules that act on HTTP exchanges; reach it as ``chaos.llm_http``.

    Inherits ``rate_limit`` (429), ``overloaded`` (529), ``server_error`` (5xx) and
    ``timeout`` from `LLMFaults`: here they become real HTTP error responses (or a read
    timeout) instead of Python exceptions, and the point name defaults to ``"llm.http"``.
    Adds `stall` and `truncate`. Build the faulty transport with `transport`.

    Example:
        ```python
        from python_wrap.chaos import FaultInjector
        from python_wrap.llm_cassette import build_response
        from python_wrap.llm_http_chaos import LLMHTTPFaults


        class Request:  # stands in for an httpx request
            method, url, headers, content = "POST", "https://llm.example/v1", {}, b"{}"


        class Wire:  # stands in for the real transport
            def handle_request(self, request):
                return build_response(request, 200, [], [b"{}"], asynchronous=False)


        faults = LLMHTTPFaults(FaultInjector())
        wire = faults.transport(Wire())
        with faults.rate_limit(times=1, retry_after=2):
            first = wire.handle_request(Request())
        assert first.status_code == 429 and first.headers["retry-after"] == "2"
        assert wire.handle_request(Request()).status_code == 200
        ```
    """

    def rate_limit(
        self,
        name: str = POINT,
        *,
        retry_after: float | None = 1.0,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Rate limit exceeded",
    ) -> Fault:
        """Answer with HTTP 429 and a ``Retry-After`` header of ``retry_after`` seconds."""
        return super().rate_limit(
            name, retry_after=retry_after, rate=rate, times=times, message=message
        )

    def overloaded(
        self,
        name: str = POINT,
        *,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Overloaded",
    ) -> Fault:
        """Answer with HTTP 529."""
        return super().overloaded(name, rate=rate, times=times, message=message)

    def server_error(
        self,
        name: str = POINT,
        *,
        status: int = 500,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Internal server error",
    ) -> Fault:
        """Answer with a 5xx ``status``."""
        return super().server_error(name, status=status, rate=rate, times=times, message=message)

    def timeout(
        self,
        name: str = POINT,
        *,
        after: float | None = None,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Request timed out",
    ) -> Fault:
        """Raise a read timeout (``httpx.ReadTimeout``, else ``TimeoutError``), optionally late."""
        return super().timeout(name, after=after, rate=rate, times=times, message=message)

    def stall(
        self,
        name: str = POINT,
        *,
        seconds: float = 30.0,
        after_chunks: int | None = None,
        rate: float = 1.0,
        times: int | None = None,
    ) -> Fault:
        """Hang for ``seconds``: before the response, or after ``after_chunks`` stream chunks.

        Time passes through ``python_wrap._core``, so ``fake_clock`` skips the wait.
        """
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        if after_chunks is None:
            return self._injector.inject(name, latency=float(seconds), rate=rate, times=times)
        if after_chunks < 0:
            raise ValueError("after_chunks must be >= 0")
        return self._injector.inject(
            name, result=_StallStream(after_chunks, float(seconds)), rate=rate, times=times
        )

    def truncate(
        self, name: str = POINT, *, keep: float = 0.5, rate: float = 1.0, times: int | None = None
    ) -> Fault:
        """Let the real response through but cut its body to the first ``keep`` fraction."""
        if not 0 <= keep < 1:
            raise ValueError("keep must be in [0, 1)")
        return self._injector.inject(name, result=_Truncate(keep), rate=rate, times=times)

    def transport(self, inner: Any, *, point: str = POINT) -> ChaosTransport:
        """Wrap ``inner`` so this injector's rules for ``point`` apply to its traffic."""
        return ChaosTransport(inner, self._injector, point=point)


def _error_response(request: Any, exc: APIStatusError, asynchronous: bool) -> Any:
    status = int(exc.status_code or 500)
    headers = [("content-type", "application/json")]
    if exc.retry_after is not None:
        shown = exc.retry_after
        headers.append(
            ("retry-after", str(int(shown)) if float(shown).is_integer() else str(shown))
        )
    kind = _ERROR_TYPES.get(status, "api_error")
    body = json.dumps({"type": "error", "error": {"type": kind, "message": str(exc)}})
    return build_response(request, status, headers, [body.encode()], asynchronous=asynchronous)


def _timeout_error(request: Any, message: str) -> Exception:
    mod = _httpx()
    if mod is not None:
        error: Exception = mod.ReadTimeout(message, request=request)
        return error
    return TimeoutError(message)


class _Stalling:
    def __init__(self, inner: Any, after_chunks: int, seconds: float) -> None:
        self._inner, self._after, self._seconds = inner, after_chunks, seconds

    def __iter__(self) -> Iterator[bytes]:
        for count, chunk in enumerate(self._inner):
            if count == self._after:
                _core.sleep(self._seconds)
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        count = 0
        async for chunk in self._inner:
            if count == self._after:
                await _core.async_sleep(self._seconds)
            count += 1
            yield chunk

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if callable(aclose):
            await aclose()


class ChaosTransport(TransportContext):
    """A transport that breaks another one on demand; build it with ``chaos.llm_http.transport``.

    Args:
        inner: The transport to wrap (anything with ``handle_request`` /
            ``handle_async_request``).
        injector: The `FaultInjector` whose rules apply.
        point: The fault point name rules match; ``"llm.http"`` by default.
    """

    def __init__(self, inner: Any, injector: FaultInjector, *, point: str = POINT) -> None:
        self.inner, self.injector, self.point = inner, injector, point

    def _wrapped(self) -> Any:
        return self.inner

    def _plan(self) -> list[Any]:
        inj = self.injector
        return inj._plan(self.point) if inj._rules and inj.enabled else []

    def _early(self, request: Any, action: Any, asynchronous: bool) -> tuple[Any, Any]:
        """Handle an exception/result action: ``(response, post_processing_marker)``."""
        if action.kind == "exception":
            exc = action.exception
            if isinstance(exc, APIStatusError):
                return _error_response(request, exc, asynchronous), None
            if isinstance(exc, APITimeoutError):
                raise _timeout_error(request, str(exc))
            raise exc
        return None, action.result

    def _finish(self, request: Any, response: Any, post: Any, asynchronous: bool) -> Any:
        if isinstance(post, _StallStream):
            response.stream = stream_class(_Stalling)(
                response.stream, post.after_chunks, post.seconds
            )
        return response

    def _truncated(
        self, request: Any, response: Any, body: bytes, keep: float, asynchronous: bool
    ) -> Any:
        headers = [
            (k, v)
            for k, v in _header_pairs(getattr(response, "headers", None))
            if k.lower() not in ("content-length", "content-encoding")
        ]
        cut = body[: int(len(body) * keep)]
        return build_response(
            request,
            int(response.status_code),
            headers,
            [cut] if cut else [],
            asynchronous=asynchronous,
        )

    def handle_request(self, request: Any) -> Any:
        """Apply matching rules, then forward the request (sync)."""
        post = None
        for action in self._plan():
            _emit(self.point, action)
            if action.kind == "latency":
                _core.sleep(action.seconds)
                continue
            response, post = self._early(request, action, False)
            if response is not None:
                return response
        response = self.inner.handle_request(request)
        if isinstance(post, _Truncate):
            return self._truncated(request, response, bytes(response.read()), post.keep, False)
        return self._finish(request, response, post, False)

    async def handle_async_request(self, request: Any) -> Any:
        """Apply matching rules, then forward the request (async)."""
        post = None
        for action in self._plan():
            _emit(self.point, action)
            if action.kind == "latency":
                await _core.async_sleep(action.seconds)
                continue
            response, post = self._early(request, action, True)
            if response is not None:
                return response
        response = await self.inner.handle_async_request(request)
        if isinstance(post, _Truncate):
            reader = getattr(response, "aread", None)
            body = bytes(await reader() if callable(reader) else response.read())
            return self._truncated(request, response, body, post.keep, True)
        return self._finish(request, response, post, True)

    def close(self) -> None:
        """Close the inner transport."""
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        """Close the inner transport (async)."""
        aclose = getattr(self.inner, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            self.close()


def _emit(point: str, action: Any) -> None:
    _core.emit("chaos.injected", point, kind=action.kind, point=point, pattern=action.pattern)
