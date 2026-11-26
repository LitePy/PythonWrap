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

import contextlib
import importlib
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from python_wrap import _core
from python_wrap.deadline import remaining as _deadline_remaining
from python_wrap.retry import RetryBudget, _compute_delay, http_status, retry, retry_after_header

__all__ = [
    "IDEMPOTENT_METHODS",
    "RETRYABLE_STATUSES",
    "AsyncPolicyTransport",
    "AsyncRetryTransport",
    "HttpPolicy",
    "HttpRetryPolicy",
    "PolicyTransport",
    "RetryTransport",
    "http_retry",
    "mount_policy",
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
    jitter: float | str = 0.0
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
        if self.jitter not in ("full", "equal") and (
            isinstance(self.jitter, str)
            or not isinstance(self.jitter, (int, float))
            or self.jitter < 0
        ):
            raise ValueError(f"jitter must be >= 0, 'full' or 'equal', got {self.jitter!r}")
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
    return _compute_delay(
        max(attempt, 1), policy.delay, policy.backoff, policy.max_delay, policy.jitter
    )


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


class _ServerError(Exception):
    """Carries a 5xx response through a circuit breaker so it counts as a failure."""

    def __init__(self, response: Any) -> None:
        super().__init__(_status_of(response))
        self.response = response


@dataclass(frozen=True)
class HttpPolicy:
    """Retries plus a circuit breaker, rate limiter, bulkhead and timeouts for HTTP calls.

    Attributes:
        retry: The `HttpRetryPolicy` (method safety, backoff, ``Retry-After``).
        breaker: ``True`` (5 failures, 30 s), a `CircuitBreaker`, or with ``per_host`` a
            factory ``name -> CircuitBreaker``. 5xx responses and transport errors count
            as failures; 4xx responses never do.
        limiter: A `RateLimiter`, or with ``per_host`` a factory ``name -> RateLimiter``.
            A 429 ``Retry-After`` pauses later calls to that host instead of failing them.
        bulkhead: A `Bulkhead`, or with ``per_host`` a factory ``name -> Bulkhead``.
        connect_timeout: Used when the caller set no connect timeout.
        read_timeout: Used when the caller set no read timeout.
        per_host: Give every host its own breaker, limiter and bulkhead, named
            ``http:<host>``, built from the factories.
        shared: A Redis client or `python_wrap.shared.SharedBackends` for the breakers
            this policy builds with ``breaker=True`` (default: ``configure(shared=...)``).
        propagate: Send ``X-Request-ID`` and the remaining ``X-Request-Deadline`` (in
            milliseconds) on every attempt, so the receiving service sees both.

    Every attempt's timeouts are also shortened to the surrounding `deadline`.

    Example:
        >>> policy = HttpPolicy(breaker=True, per_host=True)
        >>> policy.guards("a.example")[0] is policy.guards("a.example")[0]
        True
        >>> policy.guards("a.example")[0] is policy.guards("b.example")[0]
        False
    """

    retry: HttpRetryPolicy = field(default_factory=HttpRetryPolicy)
    breaker: Any = None
    limiter: Any = None
    bulkhead: Any = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    per_host: bool = False
    shared: Any = None
    propagate: bool = False
    _hosts: dict[str, tuple[Any, Any, Any]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _blocked: dict[str, float] = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("connect_timeout", "read_timeout"):
            value = getattr(self, name)
            if value is not None and not value > 0:
                raise ValueError(f"{name} must be > 0, got {value!r}")
        for name in ("limiter", "bulkhead"):
            if getattr(self, name) is True:
                raise ValueError(f"{name}=True is not allowed: pass the object or a factory")
        if self.per_host:
            from python_wrap.bulkhead import Bulkhead
            from python_wrap.circuit import CircuitBreaker
            from python_wrap.concurrency import RateLimiter

            for name in ("breaker", "limiter", "bulkhead"):
                value = getattr(self, name)
                if isinstance(value, (CircuitBreaker, RateLimiter, Bulkhead)):
                    raise ValueError(
                        f"per_host=True needs a factory for {name}= (name -> object), "
                        "not one shared instance"
                    )

    # -- per-host state ----------------------------------------------------------------
    def _build(self, kind: str, value: Any, name: str) -> Any:
        if value is None or (not callable(value) and value is not True):
            return value
        if value is True:  # only the breaker has a default
            from python_wrap.circuit import CircuitBreaker
            from python_wrap.shared import resolve_shared

            sb = resolve_shared(self.shared)
            return CircuitBreaker(
                failure_threshold=5,
                recovery_timeout=30.0,
                name=name,
                backend=None if sb is None else sb.breaker(),
            )
        from python_wrap.bulkhead import Bulkhead
        from python_wrap.circuit import CircuitBreaker
        from python_wrap.concurrency import RateLimiter

        if isinstance(value, (CircuitBreaker, RateLimiter, Bulkhead)):
            return value
        return value(name)

    def guards(self, host: str) -> tuple[Any, Any, Any]:
        """The ``(breaker, limiter, bulkhead)`` for ``host`` (one set unless ``per_host``)."""
        key = host if self.per_host else ""
        with self._lock:
            found = self._hosts.get(key)
            if found is None:
                name = f"http:{host}" if self.per_host else "http"
                found = tuple(
                    self._build(kind, getattr(self, kind), name)
                    for kind in ("breaker", "limiter", "bulkhead")
                )
                self._hosts[key] = found
            return found

    # -- helpers shared by the sync and async paths ------------------------------------
    def _blocked_wait(self, host: str) -> float:
        with self._lock:
            until = self._blocked.get(host, 0.0)
        wait = until - _core.monotonic()
        if wait <= 0:
            return 0.0
        left = _deadline_remaining()
        if wait > self.retry.max_retry_after or (left is not None and wait >= left):
            from python_wrap.errors import RateLimitExceeded

            raise RateLimitExceeded(f"{host} asked clients to wait {wait:.1f}s", retry_after=wait)
        return wait

    def _note(self, host: str, response: Any) -> None:
        if self.limiter is None or _status_of(response) != 429:
            return
        hinted = retry_after_header(response)
        if hinted:
            with self._lock:
                self._blocked[host] = _core.monotonic() + hinted

    def _request_headers(self, headers: Any) -> None:
        """Add the request id and the remaining deadline to an outgoing header mapping."""
        from python_wrap.propagation import outgoing_headers

        for name, value in outgoing_headers().items():
            headers[name] = value

    def _prepare_httpx(self, request: Any) -> None:
        extensions = getattr(request, "extensions", None)
        if isinstance(extensions, dict):
            current = dict(extensions.get("timeout") or {})
            for key, default in (("connect", self.connect_timeout), ("read", self.read_timeout)):
                if current.get(key) is None and default is not None:
                    current[key] = default
            left = _deadline_remaining()
            if left is not None:
                for key in ("connect", "read", "write", "pool"):
                    value = current.get(key)
                    current[key] = (
                        max(left, 0.001) if value is None else min(value, max(left, 0.001))
                    )
            if current:
                extensions["timeout"] = current
        if self.propagate:
            self._request_headers(request.headers)

    def requests_timeout(self, timeout: Any) -> Any:
        """The ``timeout=`` for a ``requests`` call: defaults filled in, capped by the deadline."""
        if isinstance(timeout, tuple):
            connect, read = timeout
        else:
            connect = read = timeout
        connect = self.connect_timeout if connect is None else connect
        read = self.read_timeout if read is None else read
        left = _deadline_remaining()
        if left is not None:
            left = max(left, 0.001)
            connect = left if connect is None else min(connect, left)
            read = left if read is None else min(read, left)
        return None if connect is None and read is None else (connect, read)

    def call(self, host: str, send: Callable[[], Any]) -> Any:
        """One attempt through the guards: limiter, bulkhead, breaker, then ``send()``."""
        breaker, limiter, bulkhead = self.guards(host)
        if limiter is not None:
            wait = self._blocked_wait(host)
            if wait:
                _core.sleep(wait)

        def counted() -> Any:
            response = send()
            status = _status_of(response)
            if breaker is not None and status is not None and status >= 500:
                raise _ServerError(response)
            return response

        try:
            with contextlib.ExitStack() as stack:
                if limiter is not None:
                    stack.enter_context(limiter)
                if bulkhead is not None:
                    stack.enter_context(bulkhead)
                response = breaker.call(counted) if breaker is not None else counted()
        except _ServerError as exc:
            response = exc.response
        self._note(host, response)
        return response

    async def acall(self, host: str, send: Callable[[], Awaitable[Any]]) -> Any:
        """`call` for coroutines."""
        breaker, limiter, bulkhead = self.guards(host)
        if limiter is not None:
            wait = self._blocked_wait(host)
            if wait:
                await _core.async_sleep(wait)

        async def counted() -> Any:
            response = await send()
            status = _status_of(response)
            if breaker is not None and status is not None and status >= 500:
                raise _ServerError(response)
            return response

        try:
            async with contextlib.AsyncExitStack() as stack:
                if limiter is not None:
                    await stack.enter_async_context(limiter)
                if bulkhead is not None:
                    await stack.enter_async_context(bulkhead)
                response = await breaker.call(counted) if breaker is not None else await counted()
        except _ServerError as exc:
            response = exc.response
        self._note(host, response)
        return response


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


class PolicyTransport:
    """A synchronous httpx transport that applies a whole `HttpPolicy`.

    Retries (method-safe), a circuit breaker, rate limiter and bulkhead (optionally per
    host), default timeouts shortened to the surrounding deadline, and optional
    request-id / deadline headers. Wrap any object with httpx's transport interface.

    Args:
        inner: The transport to wrap, e.g. ``httpx.HTTPTransport()``.
        policy: The `HttpPolicy`; retries only with its defaults when omitted.

    Example:
        ```python
        import httpx
        from python_wrap.http import HttpPolicy, PolicyTransport

        policy = HttpPolicy(breaker=True, per_host=True, read_timeout=10)
        client = httpx.Client(transport=PolicyTransport(httpx.HTTPTransport(), policy))
        ```
    """

    def __init__(self, inner: Any, policy: HttpPolicy | None = None) -> None:
        self.inner = inner
        self.policy: Any = policy or HttpPolicy()

    @property
    def _full(self) -> HttpPolicy:
        return self.policy  # type: ignore[no-any-return]

    def _send(self, request: Any) -> Any:
        self._full._prepare_httpx(request)
        return self.inner.handle_request(request)

    def handle_request(self, request: Any) -> Any:
        """Send `request`, applying the policy."""
        full = self._full
        host = _host(request.url)
        run = _Run(full.retry, request.method, request.headers, host)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = full.call(host, lambda: self._send(request))
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

    def __enter__(self) -> PolicyTransport:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class RetryTransport(PolicyTransport):
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
        retry_policy = policy or HttpRetryPolicy()
        super().__init__(inner, HttpPolicy(retry=retry_policy))
        self._full_policy = self.policy
        self.policy = retry_policy

    @property
    def _full(self) -> HttpPolicy:
        return self._full_policy  # type: ignore[no-any-return]

    def __enter__(self) -> RetryTransport:
        return self


class AsyncPolicyTransport:
    """The asyncio counterpart of `PolicyTransport` for ``httpx.AsyncClient``.

    Args:
        inner: The async transport to wrap, e.g. ``httpx.AsyncHTTPTransport()``.
        policy: The `HttpPolicy`; retries only with its defaults when omitted.
    """

    def __init__(self, inner: Any, policy: HttpPolicy | None = None) -> None:
        self.inner = inner
        self.policy: Any = policy or HttpPolicy()

    @property
    def _full(self) -> HttpPolicy:
        return self.policy  # type: ignore[no-any-return]

    async def _send(self, request: Any) -> Any:
        self._full._prepare_httpx(request)
        return await self.inner.handle_async_request(request)

    async def handle_async_request(self, request: Any) -> Any:
        """Send `request`, applying the policy."""
        full = self._full
        host = _host(request.url)
        run = _Run(full.retry, request.method, request.headers, host)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await full.acall(host, lambda: self._send(request))
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

    async def __aenter__(self) -> AsyncPolicyTransport:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()


class AsyncRetryTransport(AsyncPolicyTransport):
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
        retry_policy = policy or HttpRetryPolicy()
        super().__init__(inner, HttpPolicy(retry=retry_policy))
        self._full_policy = self.policy
        self.policy = retry_policy

    @property
    def _full(self) -> HttpPolicy:
        return self._full_policy  # type: ignore[no-any-return]

    async def __aenter__(self) -> AsyncRetryTransport:
        return self


def mount_policy(
    session: Any,
    policy: HttpPolicy | None = None,
    prefixes: Iterable[str] = ("http://", "https://"),
) -> Any:
    """Mount an adapter that applies a whole `HttpPolicy` on a ``requests.Session``.

    ``requests`` is imported lazily. The adapter subclasses
    ``requests.adapters.HTTPAdapter`` and wraps its ``send`` with the policy, so it
    replaces any adapter already mounted on those prefixes. Default timeouts apply when
    the call passed none (``requests`` has none by default) and are shortened to the
    surrounding deadline.

    Args:
        session: A ``requests.Session`` (anything with ``mount``).
        policy: The `HttpPolicy`; retries only with its defaults when omitted.
        prefixes: URL prefixes to mount on.

    Returns:
        The mounted adapter (its ``http_policy`` attribute holds the policy).

    Raises:
        ImportError: If ``requests`` is not installed.

    Example:
        ```python
        import requests
        from python_wrap.http import HttpPolicy, mount_policy

        session = requests.Session()
        mount_policy(session, HttpPolicy(breaker=True, per_host=True, read_timeout=10))
        ```
    """
    adapters = importlib.import_module("requests.adapters")
    base: Any = adapters.HTTPAdapter
    resolved = policy or HttpPolicy()

    class PolicyAdapter(base):  # type: ignore[misc]
        http_policy = resolved
        retry_policy = resolved.retry

        def send(self, request: Any, **kwargs: Any) -> Any:
            full = self.http_policy
            host = _host(request.url)
            run = _Run(full.retry, request.method, request.headers, host)

            def once() -> Any:
                full_timeout = full.requests_timeout(kwargs.get("timeout"))
                options = dict(kwargs)
                if full_timeout is not None:
                    options["timeout"] = full_timeout
                if full.propagate:
                    full._request_headers(request.headers)
                return super(PolicyAdapter, self).send(request, **options)

            attempt = 0
            while True:
                attempt += 1
                try:
                    response = full.call(host, once)
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

    adapter = PolicyAdapter()
    for prefix in prefixes:
        session.mount(prefix, adapter)
    return adapter


def mount_retries(
    session: Any,
    policy: HttpRetryPolicy | None = None,
    prefixes: Iterable[str] = ("http://", "https://"),
) -> Any:
    """Mount a retrying adapter on a ``requests.Session`` (retries only).

    A thin wrapper over `mount_policy` with ``HttpPolicy(retry=policy)``; the returned
    adapter's ``retry_policy`` attribute holds the retry policy.

    Args:
        session: A ``requests.Session`` (anything with ``mount``).
        policy: The retry policy; defaults apply when omitted.
        prefixes: URL prefixes to mount on.

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
    return mount_policy(session, HttpPolicy(retry=policy or HttpRetryPolicy()), prefixes)


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
