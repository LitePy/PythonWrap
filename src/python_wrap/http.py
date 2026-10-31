"""HTTP client integration: retry policy, httpx transports, requests adapter, generic decorator.

Everything here is duck-typed or imports its third-party library lazily, so
``python_wrap`` keeps zero runtime dependencies. The decision logic lives in two pure
helpers, `should_retry` and `retry_delay`, which every integration shares.

Method safety is the core rule: a request is retried only when its method is idempotent
(GET, HEAD, OPTIONS, PUT, DELETE, TRACE) or it carries an idempotency header
(``Idempotency-Key`` by default). A bare POST or PATCH is never retried, neither on a
retryable status nor on a transport error, because the server may already have acted.
"""

from __future__ import annotations

import importlib
import logging
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from python_wrap import _core
from python_wrap.deadline import remaining as _deadline_remaining
from python_wrap.retry import RetryBudget, http_status, retry, retry_after_header

__all__ = [
    "IDEMPOTENT_METHODS",
    "RETRYABLE_STATUSES",
    "AsyncRetryTransport",
    "HttpRetryPolicy",
    "RetryTransport",
    "http_retry",
    "mount_retries",
    "retry_delay",
    "should_retry",
]

_log = logging.getLogger("python_wrap")

IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
"""Methods that are safe to repeat (RFC 9110)."""

RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
"""Statuses that usually mean "try again": timeouts, throttling and server-side failures."""

_TRANSPORT_NAMES: frozenset[str] = frozenset(
    {
        "TransportError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "Timeout",
        "ClientConnectionError",
        "ServerDisconnectedError",
        "ClientOSError",
    }
)


@dataclass(frozen=True)
class HttpRetryPolicy:
    """What to retry, how long to wait, and when to stop, for HTTP calls.

    Attributes:
        max_attempts: Total tries including the first (>= 1).
        delay: Base wait before the second attempt, in seconds.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Cap on the backoff wait (before jitter).
        jitter: Up to this many random extra seconds are added to a backoff wait.
        statuses: Response statuses that trigger a retry.
        methods: Methods considered safe to repeat (upper-case).
        retry_on: Exception types treated as transport failures.
        retry_on_names: Class names matched along an exception's MRO, so httpx, requests
            and aiohttp errors are recognised without importing those libraries.
        respect_retry_after: Use a ``Retry-After`` header as the exact wait.
        max_retry_after: A ``Retry-After`` larger than this means "do not retry": the
            response is returned (or the error raised) instead.
        budget: Optional shared `RetryBudget`; when it refuses, retrying stops.
        idempotency_header: A request carrying this header may be retried whatever its
            method.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        >>> policy = HttpRetryPolicy(max_attempts=4, delay=0.2)
        >>> should_retry("GET", {}, status=503, policy=policy)
        True
        >>> should_retry("POST", {}, status=503, policy=policy)
        False
        >>> should_retry("POST", {"Idempotency-Key": "k1"}, status=503, policy=policy)
        True
    """

    max_attempts: int = 3
    delay: float = 0.5
    backoff: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.0
    statuses: frozenset[int] = RETRYABLE_STATUSES
    methods: frozenset[str] = IDEMPOTENT_METHODS
    retry_on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)
    retry_on_names: frozenset[str] = _TRANSPORT_NAMES
    respect_retry_after: bool = True
    max_retry_after: float = 60.0
    budget: RetryBudget | None = field(default=None, compare=False)
    idempotency_header: str | None = "Idempotency-Key"

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts!r}")
        if not self.delay >= 0:
            raise ValueError(f"delay must be >= 0, got {self.delay!r}")
        if not self.backoff >= 1:
            raise ValueError(f"backoff must be >= 1, got {self.backoff!r}")
        if not self.max_delay >= 0:
            raise ValueError(f"max_delay must be >= 0, got {self.max_delay!r}")
        if not self.jitter >= 0:
            raise ValueError(f"jitter must be >= 0, got {self.jitter!r}")
        if not self.max_retry_after >= 0:
            raise ValueError(f"max_retry_after must be >= 0, got {self.max_retry_after!r}")
        if self.budget is not None and not isinstance(self.budget, RetryBudget):
            raise TypeError("budget must be a RetryBudget or None")
        object.__setattr__(self, "statuses", frozenset(self.statuses))
        object.__setattr__(self, "methods", frozenset(m.upper() for m in self.methods))
        object.__setattr__(self, "retry_on", tuple(self.retry_on))
        object.__setattr__(self, "retry_on_names", frozenset(self.retry_on_names))


_DEFAULT_POLICY = HttpRetryPolicy()


def _has_header(headers: Any, name: str | None) -> bool:
    if not name or headers is None:
        return False
    try:
        if headers.get(name) is not None:
            return True
    except Exception:
        pass
    try:
        wanted = name.lower()
        items = headers.items() if hasattr(headers, "items") else headers
        return any(str(k).lower() == wanted for k, _ in items)
    except Exception:
        return False


def _method_allowed(method: str, headers: Any, policy: HttpRetryPolicy) -> bool:
    if str(method).upper() in policy.methods:
        return True
    return _has_header(headers, policy.idempotency_header)


def _is_transport_error(exc: BaseException, policy: HttpRetryPolicy) -> bool:
    if isinstance(exc, policy.retry_on):
        return True
    return any(cls.__name__ in policy.retry_on_names for cls in type(exc).__mro__)


def _status_of(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def should_retry(
    method: str,
    headers: Mapping[str, str] | Any,
    status: int | None = None,
    exc: BaseException | None = None,
    policy: HttpRetryPolicy | None = None,
) -> bool:
    """Decide whether a failed HTTP attempt may be retried (pure; no attempt counting).

    Args:
        method: The request method, any case.
        headers: The request headers (any mapping-like, case-insensitive or not).
        status: The response status, if a response came back.
        exc: The exception, if the call raised.
        policy: The policy; the defaults when omitted.

    Returns:
        `True` if the method is safe to repeat (idempotent or carrying the idempotency
        header) and the status is retryable or the exception is a transport error.
        With neither `status` nor `exc`, `False`.

    Example:
        >>> should_retry("GET", {}, exc=ConnectionError())
        True
        >>> should_retry("POST", {}, exc=ConnectionError())
        False
        >>> should_retry("GET", {}, status=404)
        False
    """
    policy = policy or _DEFAULT_POLICY
    if not _method_allowed(method, headers, policy):
        return False
    if exc is not None:
        return _is_transport_error(exc, policy)
    return status is not None and status in policy.statuses


def retry_delay(
    response_or_exc: Any, attempt: int, policy: HttpRetryPolicy | None = None
) -> float | None:
    """Seconds to wait after failed attempt number `attempt` (1-based).

    A ``Retry-After`` on the response (or on ``exc.response``) wins and is used exactly;
    otherwise the capped exponential backoff plus jitter applies.

    Args:
        response_or_exc: The failed response or exception.
        attempt: The attempt that just failed, starting at 1.
        policy: The policy; the defaults when omitted.

    Returns:
        The wait in seconds, or `None` when the server asked for longer than
        `policy.max_retry_after` (the caller should stop retrying).

    Example:
        >>> class Resp:
        ...     headers = {"Retry-After": "2"}
        >>> retry_delay(Resp(), 1)
        2.0
        >>> retry_delay(object(), 3, HttpRetryPolicy(delay=1, backoff=2))
        4.0
    """
    policy = policy or _DEFAULT_POLICY
    if policy.respect_retry_after:
        hinted = retry_after_header(response_or_exc)
        if hinted is not None:
            return None if hinted > policy.max_retry_after else hinted
    try:
        wait = policy.delay * policy.backoff ** max(attempt - 1, 0)
    except OverflowError:
        wait = policy.max_delay
    wait = min(wait, policy.max_delay)
    if policy.jitter:
        wait += random.uniform(0, policy.jitter)
    return float(wait)


class _Run:
    """One logical call: shared decision, budget and event logic for every integration."""

    def __init__(self, policy: HttpRetryPolicy, method: str, headers: Any, host: str) -> None:
        self.policy = policy
        self.method = str(method).upper()
        self.headers = headers
        self.name = f"http {self.method} {host}".rstrip()
        if policy.budget is not None:
            policy.budget.deposit()

    def next_delay(
        self, attempt: int, response: Any = None, exc: BaseException | None = None
    ) -> float | None:
        """The wait before the next attempt, or `None` to stop (response/exception stands)."""
        policy = self.policy
        subject = exc if exc is not None else response
        status = _status_of(response) if exc is None else None
        if not should_retry(self.method, self.headers, status, exc, policy):
            return None
        fields: dict[str, Any] = {"attempts": attempt, "exception": exc, "status": status}
        if attempt >= policy.max_attempts:
            _core.emit("retry.gave_up", self.name, **fields)
            return None
        wait = retry_delay(subject, attempt, policy)
        if wait is None:
            _core.emit("retry.gave_up", self.name, **fields)
            return None
        left = _deadline_remaining()
        if left is not None and wait >= left:
            _core.emit("retry.gave_up", self.name, **fields)
            return None
        if policy.budget is not None and not policy.budget.try_withdraw():
            _core.emit("retry.budget_exhausted", self.name, **fields)
            _core.emit("retry.gave_up", self.name, **fields)
            return None
        _core.emit(
            "retry.attempt_failed",
            self.name,
            attempt=attempt,
            exception=exc,
            status=status,
            next_delay=wait,
        )
        return wait

    def succeeded(self, attempt: int) -> None:
        if attempt > 1:
            _core.emit("retry.succeeded", self.name, attempts=attempt)


def _close(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            _log.debug("closing a discarded response failed", exc_info=True)


async def _aclose(response: Any) -> None:
    closer = getattr(response, "aclose", None)
    if callable(closer):
        try:
            result = closer()
            if isinstance(result, Awaitable):
                await result
        except Exception:
            _log.debug("closing a discarded response failed", exc_info=True)
        return
    _close(response)


def _host(url: Any) -> str:
    host = getattr(url, "host", None)
    if isinstance(host, str):
        return host
    if isinstance(url, str):
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or ""
    return ""


class RetryTransport:
    """A synchronous httpx transport wrapper that retries per an `HttpRetryPolicy`.

    Wrap any object with httpx's transport interface (``handle_request`` and
    ``close``). Discarded responses are closed before the next attempt. The request body
    must be replayable (bytes or str content; not a one-shot stream).

    Args:
        inner: The transport to wrap, e.g. ``httpx.HTTPTransport()``.
        policy: The retry policy; defaults apply when omitted.

    Example:
        ```python
        import httpx
        from python_wrap.http import HttpRetryPolicy, RetryTransport

        client = httpx.Client(
            transport=RetryTransport(httpx.HTTPTransport(), policy=HttpRetryPolicy())
        )
        ```
    """

    def __init__(self, inner: Any, policy: HttpRetryPolicy | None = None) -> None:
        self.inner = inner
        self.policy = policy or HttpRetryPolicy()

    def handle_request(self, request: Any) -> Any:
        """Send `request`, retrying as the policy allows."""
        run = _Run(self.policy, request.method, request.headers, _host(request.url))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.inner.handle_request(request)
            except Exception as exc:
                wait = run.next_delay(attempt, exc=exc)
                if wait is None:
                    raise
            else:
                wait = run.next_delay(attempt, response=response)
                if wait is None:
                    run.succeeded(attempt)
                    return response
                _close(response)
            _core.sleep(wait)

    def close(self) -> None:
        """Close the wrapped transport."""
        closer = getattr(self.inner, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> RetryTransport:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class AsyncRetryTransport:
    """The asyncio counterpart of `RetryTransport` for ``httpx.AsyncClient``.

    Args:
        inner: The async transport to wrap, e.g. ``httpx.AsyncHTTPTransport()``.
        policy: The retry policy; defaults apply when omitted.

    Example:
        ```python
        import httpx
        from python_wrap.http import AsyncRetryTransport

        client = httpx.AsyncClient(transport=AsyncRetryTransport(httpx.AsyncHTTPTransport()))
        ```
    """

    def __init__(self, inner: Any, policy: HttpRetryPolicy | None = None) -> None:
        self.inner = inner
        self.policy = policy or HttpRetryPolicy()

    async def handle_async_request(self, request: Any) -> Any:
        """Send `request`, retrying as the policy allows."""
        run = _Run(self.policy, request.method, request.headers, _host(request.url))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self.inner.handle_async_request(request)
            except Exception as exc:
                wait = run.next_delay(attempt, exc=exc)
                if wait is None:
                    raise
            else:
                wait = run.next_delay(attempt, response=response)
                if wait is None:
                    run.succeeded(attempt)
                    return response
                await _aclose(response)
            await _core.async_sleep(wait)

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        closer = getattr(self.inner, "aclose", None)
        if callable(closer):
            result = closer()
            if isinstance(result, Awaitable):
                await result

    async def __aenter__(self) -> AsyncRetryTransport:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()


def mount_retries(
    session: Any,
    policy: HttpRetryPolicy | None = None,
    prefixes: Iterable[str] = ("http://", "https://"),
) -> Any:
    """Mount a retrying adapter on a ``requests.Session``.

    ``requests`` is imported lazily here. The adapter subclasses
    ``requests.adapters.HTTPAdapter`` and wraps its ``send`` with the retry loop, so it
    replaces any adapter already mounted on those prefixes.

    Args:
        session: A ``requests.Session`` (anything with ``mount``).
        policy: The retry policy; defaults apply when omitted.
        prefixes: URL prefixes to mount on.

    Returns:
        The mounted adapter (its ``retry_policy`` attribute holds the policy).

    Raises:
        ImportError: If ``requests`` is not installed.

    Example:
        ```python
        import requests
        from python_wrap.http import HttpRetryPolicy, mount_retries

        session = requests.Session()
        mount_retries(session, HttpRetryPolicy(max_attempts=4))
        ```
    """
    adapters = importlib.import_module("requests.adapters")
    base: Any = adapters.HTTPAdapter
    resolved = policy or HttpRetryPolicy()

    class RetryAdapter(base):  # type: ignore[misc]
        retry_policy = resolved

        def send(self, request: Any, **kwargs: Any) -> Any:
            run = _Run(self.retry_policy, request.method, request.headers, _host(request.url))
            attempt = 0
            while True:
                attempt += 1
                try:
                    response = super().send(request, **kwargs)
                except Exception as exc:
                    wait = run.next_delay(attempt, exc=exc)
                    if wait is None:
                        raise
                else:
                    wait = run.next_delay(attempt, response=response)
                    if wait is None:
                        run.succeeded(attempt)
                        return response
                    _close(response)
                _core.sleep(wait)

    adapter = RetryAdapter()
    for prefix in prefixes:
        session.mount(prefix, adapter)
    return adapter


def http_retry(
    policy: HttpRetryPolicy | None = None,
    *,
    method: str = "GET",
    idempotent: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate any function or coroutine that returns a response-like object.

    Built on `retry`: a result with a retryable ``.status``/``.status_code`` is retried,
    waiting for its ``Retry-After`` when present, as are transport errors. Works for
    aiohttp and hand-written clients. Because the function's own arguments are opaque,
    declare the method here.

    A non-idempotent `method` is **not retried at all** (the function is returned
    unchanged) unless `idempotent=True` asserts the call is safe, e.g. because it sends
    an idempotency key.

    Args:
        policy: The retry policy; defaults apply when omitted.
        method: The HTTP method the decorated function performs.
        idempotent: Override the method-based decision: `True` allows retries, `False`
            forbids them. `None` decides from `method` and `policy.methods`.

    Returns:
        A decorator.

    Example:
        >>> from python_wrap.http import http_retry
        >>> class Resp:
        ...     status = 200
        ...     headers = {}
        >>> @http_retry(method="GET")
        ... def fetch():
        ...     return Resp()
        >>> fetch().status
        200
    """
    resolved = policy or HttpRetryPolicy()
    allowed = idempotent if idempotent is not None else method.upper() in resolved.methods

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if not allowed:
            return func
        status_hit = http_status(*resolved.statuses) if resolved.statuses else None

        def too_long(value: Any) -> bool:
            if not resolved.respect_retry_after:
                return False
            hinted = retry_after_header(value)
            return hinted is not None and hinted > resolved.max_retry_after

        def result_rejected(result: Any) -> bool:
            return status_hit is not None and status_hit(result) and not too_long(result)

        def error_rejected(exc: BaseException) -> bool:
            return _is_transport_error(exc, resolved) and not too_long(exc)

        def hint(value: Any) -> float | None:
            return retry_after_header(value) if resolved.respect_retry_after else None

        steps = [
            min(resolved.delay * resolved.backoff**i, resolved.max_delay)
            for i in range(max(resolved.max_attempts - 1, 1))
        ]
        return retry(
            max_attempts=resolved.max_attempts,
            delays=steps,
            max_delay=max(resolved.max_delay, resolved.max_retry_after),
            jitter=resolved.jitter,
            on=Exception,
            retry_if=error_rejected,
            retry_if_result=result_rejected,
            delay_from=hint,
            budget=resolved.budget,
            log_level=None,
        )(func)

    return decorate
