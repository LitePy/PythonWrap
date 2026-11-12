"""Wrappers for LLM calls: classified retries, spend budgets, token rate limits,
model fallback chains, streaming timeouts and a response cache.

Provider SDKs are never imported. Responses are duck-typed: token usage is read from
``response.usage.input_tokens`` / ``output_tokens`` (Anthropic style),
``response.usage.prompt_tokens`` / ``completion_tokens`` (OpenAI style), the same keys
on a dict, or a ``usage=`` callable you supply. Errors are classified by ``status_code``
(on the exception or its ``.response``), by class name and by the error body, so the
same code works with any client library, or with your own HTTP calls.

The pieces:

- `llm_transient` / `llm_retry`: retry 408/409/429/529/5xx, overloaded, timeout and
  connection errors (honouring ``Retry-After`` and ``retry-after-ms``), never other 4xx
  or content-policy refusals. ``python_wrap.presets.llm_client`` bundles it with a
  timeout and a circuit breaker.
- `Budget` / `budget`: token and dollar limits per total, per key (user, tenant), per
  explicit run or per call chain, with an optional shared backend.
- `token_rate_limit`: a tokens-per-minute bucket that reserves an estimate before the
  call and reconciles it with the real usage afterwards.
- `fallback_models`: try models in order, each behind its own circuit breaker, with an
  optional last-good cached answer; emits ``llm.fallback_served``.
- `stream_timeout`: ``first_token=``, ``chunk_gap=`` and ``total=`` limits for sync and
  async streams; raises `StreamTimeout`.
- `llm_cache`: an exact-match response cache keyed on the request, with a pluggable
  store and an optional ``similarity=`` embedding hook for semantic matches.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import functools
import hashlib
import inspect
import json
import math
import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Hashable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core, _fork
from python_wrap._runtime import digest
from python_wrap.circuit import CircuitBreaker
from python_wrap.errors import CircuitOpenError, PythonWrapError, RateLimitExceeded, TimeoutExpired
from python_wrap.retry import RetryBudget, retry, retry_after_header

if TYPE_CHECKING:
    from python_wrap.doctor import Finding, Layer

__all__ = [
    "Budget",
    "BudgetBackend",
    "BudgetExceeded",
    "RedisBudgetBackend",
    "Spend",
    "StreamTimeout",
    "TokenRateLimiter",
    "Usage",
    "budget",
    "estimate_tokens",
    "fallback_models",
    "llm_cache",
    "llm_retry",
    "llm_retry_after",
    "llm_transient",
    "read_usage",
    "stream_timeout",
    "token_rate_limit",
]

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

_LAYER_ATTR = "__python_wrap_layer__"
_Decorator = Callable[[Callable[P, R]], Callable[P, R]]
ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


# ----------------------------------------------------------------------- exceptions


class BudgetExceeded(PythonWrapError):
    """A token or dollar `Budget` is used up, so the call was not made.

    Attributes:
        budget: The budget name.
        scope: The scope that ran out (``"total"``, ``"key"``, ``"run"``, ``"chain"``).
        tokens: Tokens spent in that scope so far.
        usd: Dollars spent in that scope so far.
        limit_tokens: The token limit, or None.
        limit_usd: The dollar limit, or None.
    """

    def __init__(
        self,
        message: str,
        *,
        budget: str,
        scope: str,
        tokens: int,
        usd: float,
        limit_tokens: int | None,
        limit_usd: float | None,
    ) -> None:
        super().__init__(message)
        self.budget = budget
        self.scope = scope
        self.tokens = tokens
        self.usd = usd
        self.limit_tokens = limit_tokens
        self.limit_usd = limit_usd


class StreamTimeout(TimeoutExpired):
    """A stream produced no chunk in time.

    Attributes:
        limit: Which limit tripped: ``"first_token"``, ``"chunk_gap"`` or ``"total"``.
        seconds: That limit's value.
    """

    def __init__(self, message: str, *, seconds: float, limit: str) -> None:
        super().__init__(message, seconds=seconds)
        self.limit = limit


# ------------------------------------------------------------ error classification

#: Class names (anywhere in the MRO) of transient errors in common LLM SDKs.
_TRANSIENT_NAMES = frozenset(
    {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
        "ServiceUnavailable",
        "TooManyRequests",
        "ResourceExhausted",
        "DeadlineExceeded",
    }
)
#: Error-body ``type``/``code`` values that mean "try again later".
_TRANSIENT_CODES = frozenset(
    {"overloaded_error", "rate_limit_error", "rate_limit_exceeded", "server_error", "timeout"}
)
#: Error-body ``type``/``code`` values that mean "the provider refused the content".
_REFUSAL_CODES = frozenset(
    {
        "content_filter",
        "content_policy_violation",
        "content_policy",
        "refusal",
        "safety",
        "responsible_ai_policy_violation",
    }
)
_REFUSAL_HINTS = ("contentfilter", "contentpolicy", "refusal", "safety", "moderation")


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping key or an attribute."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _status(exc: BaseException) -> int | None:
    for source in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "status", "http_status"):
            found = _int(_field(source, name))
            if found is not None and 100 <= found <= 599:
                return found
    return None


def _body_codes(exc: BaseException) -> set[str]:
    codes: set[str] = set()
    for name in ("code", "type"):
        value = getattr(exc, name, None)
        if isinstance(value, str):
            codes.add(value.casefold())
    body = getattr(exc, "body", None)
    for part in (body, _field(body, "error")):
        if isinstance(part, Mapping):
            for name in ("type", "code"):
                value = part.get(name)
                if isinstance(value, str):
                    codes.add(value.casefold())
    return codes


def _is_refusal(exc: BaseException) -> bool:
    names = [klass.__name__.casefold() for klass in type(exc).__mro__]
    if any(hint in name for name in names for hint in _REFUSAL_HINTS):
        return True
    return bool(_body_codes(exc) & _REFUSAL_CODES)


def llm_transient(exc: BaseException) -> bool:
    """True when an LLM call that raised ``exc`` is worth retrying.

    Retried: HTTP 408, 409, 429, 529 and every 5xx (read from ``status_code`` or
    ``status`` on the exception or its ``.response``); bodies typed
    ``overloaded_error`` / ``rate_limit_error``; SDK classes named like
    ``RateLimitError``, ``APITimeoutError``, ``APIConnectionError``,
    ``InternalServerError``; and any ``ConnectionError`` or ``TimeoutError``.

    Never retried: every other 4xx (bad request, auth, not found), content-policy or
    refusal errors (class names containing ``ContentFilter``, ``Refusal``, ``Safety``...
    or a ``content_filter`` / ``content_policy_violation`` code), `CircuitOpenError` and
    `BudgetExceeded`.

    Args:
        exc: The exception an LLM call raised.

    Returns:
        Whether a retry (or a fallback to another model) may help.

    Example:
        ```python
        from python_wrap.llm import llm_transient

        class APIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        assert llm_transient(APIStatusError(529))
        assert not llm_transient(APIStatusError(400))
        ```
    """
    if isinstance(exc, (CircuitOpenError, BudgetExceeded)):
        return False
    if _is_refusal(exc):
        return False
    status = _status(exc)
    if status is not None:
        return status in (408, 409, 429, 529) or status >= 500
    if _body_codes(exc) & _TRANSIENT_CODES:
        return True
    if any(klass.__name__ in _TRANSIENT_NAMES for klass in type(exc).__mro__):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


def llm_retry_after(exc: Any) -> float | None:
    """The wait an LLM provider asked for, for ``retry(delay_from=...)``.

    Reads a ``retry-after-ms`` header (milliseconds, sent by some providers) first, then
    everything `retry_after_header` understands (``retry_after`` attribute,
    ``Retry-After`` in seconds or as an HTTP date, on the error or ``.response``).

    Args:
        exc: An exception (or response-like object).

    Returns:
        Seconds to wait, or None when the provider gave no hint.
    """
    try:
        headers = getattr(exc, "headers", None)
        if headers is None:
            headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            items = headers.items() if hasattr(headers, "items") else headers
            for key, value in items:
                if str(key).casefold() == "retry-after-ms":
                    millis = float(value)
                    if math.isfinite(millis):
                        return max(millis / 1000, 0.0)
    except (TypeError, ValueError, AttributeError):
        pass
    return retry_after_header(exc)


def llm_retry(
    *,
    max_attempts: int = 4,
    delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: float = 0.5,
    max_time: float | None = None,
    budget: RetryBudget | None = None,
    retry_if: Callable[[BaseException], bool] | None = None,
    owns_retries: bool = False,
    client: Any = None,
) -> _Decorator[..., Any]:
    """A `retry` configured for LLM APIs.

    Retries what `llm_transient` accepts (rate limits, overloaded, 5xx, timeouts and
    connection errors) and nothing else, so a bad request or a content-policy refusal
    fails at once instead of being paid for four times. The wait doubles from
    ``delay`` up to ``max_delay`` with up to ``jitter`` random seconds, unless the
    provider sent ``Retry-After`` / ``retry-after-ms``: that wait is used exactly (still
    capped by ``max_delay``). Retries draw on a `RetryBudget` (20 % of calls plus 5/s by
    default) so an outage does not multiply your traffic.

    Args:
        max_attempts: Total attempts, including the first.
        delay: First backoff wait in seconds.
        max_delay: Longest single wait, also the cap on server hints.
        jitter: Random extra seconds added to each backoff wait.
        max_time: Total time budget for all attempts (None: no limit).
        budget: A shared `RetryBudget`; a new one (ratio 0.2, 5/s) otherwise.
        retry_if: Extra predicate; an error is retried only if it is transient **and**
            this returns True.
        owns_retries: Make this the only retry loop for the call. Any ``llm_retry``
            running inside it (in a helper, or in a guarded SDK client from
            `python_wrap.adapters`) makes a single attempt, and when this decorator
            itself runs inside another ``llm_retry`` it makes a single attempt, so
            attempts never multiply across layers (4 x 3 = 12). Turn the SDK's own
            retries off as well (`python_wrap.adapters.no_sdk_retries`).
        client: The SDK client the function calls. Only its ``max_retries`` setting is
            read, so ``doctor`` can warn (PW018) while the SDK still retries underneath.

    Returns:
        A decorator for sync or async functions.

    Example:
        ```python
        from python_wrap.llm import llm_retry
        from python_wrap.testing import fake_clock

        class Overloaded(Exception):
            status_code = 529

        calls = []

        @llm_retry(max_attempts=3)
        def complete(prompt: str) -> str:
            calls.append(prompt)
            if len(calls) < 3:
                raise Overloaded
            return "done"

        with fake_clock():
            assert complete("hi") == "done"
        assert len(calls) == 3
        ```
    """
    extra = retry_if

    def transient(exc: BaseException) -> bool:
        return llm_transient(exc) and (extra is None or bool(extra(exc)))

    inner = retry(
        max_attempts=max_attempts,
        delay=delay,
        backoff=2,
        max_delay=max_delay,
        jitter=jitter,
        max_time=max_time,
        on=Exception,
        retry_if=transient,
        delay_from=llm_retry_after,
        budget=budget if budget is not None else RetryBudget(ratio=0.2, min_per_second=5),
    )
    settings = {**getattr(inner, _LAYER_ATTR)[1], "llm": "retry", "owns_retries": owns_retries}
    sdk_retries = _sdk_retries(client) if client is not None else None
    if sdk_retries is not None:
        settings["sdk_retries"] = sdk_retries
    mark = "owner" if owns_retries else "loop"

    def single(state: str | None) -> bool:
        # Inside an owner every llm_retry makes one attempt; so does an owner inside a loop.
        return state == "owner" or (owns_retries and state is not None)

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        looped: Callable[..., Any] = inner(func)
        wrapper: Any
        if _core.is_async(func):
            afunc = cast("Callable[..., Awaitable[Any]]", func)

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _RETRY_SCOPE.get()
                if single(state):
                    return await afunc(*args, **kwargs)
                token = _RETRY_SCOPE.set(state if state == "owner" else mark)
                try:
                    return await looped(*args, **kwargs)
                finally:
                    _RETRY_SCOPE.reset(token)

            wrapper = async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _RETRY_SCOPE.get()
                if single(state):
                    return func(*args, **kwargs)
                token = _RETRY_SCOPE.set(state if state == "owner" else mark)
                try:
                    return looped(*args, **kwargs)
                finally:
                    _RETRY_SCOPE.reset(token)

            wrapper = sync_wrapper
        stats = getattr(looped, "__retry_stats__", None)
        if stats is not None:
            wrapper.__retry_stats__ = stats
        setattr(wrapper, _LAYER_ATTR, ("retry", settings))
        return cast("Callable[P, R]", wrapper)

    setattr(decorate, _LAYER_ATTR, ("retry", settings))
    return decorate


#: None outside any `llm_retry`, ``"loop"`` inside one, ``"owner"`` inside an
#: ``owns_retries=True`` one (where nested retries make a single attempt).
_RETRY_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "python_wrap_llm_retry_scope", default=None
)


def _sdk_retries(client: Any) -> int | None:
    """The ``max_retries`` an SDK client (or a guard around one) is set to, or None."""
    for source in (client, getattr(client, "_client", None)):
        value = _int(getattr(source, "max_retries", None))
        if value is not None:
            return max(value, 0)
    return None


# --------------------------------------------------- breakers counting transients only


class _NotCounted(Exception):
    """Carries an error past a breaker without counting it as a failure."""

    def __init__(self, error: Exception) -> None:
        super().__init__(repr(error))
        self.error = error


def _llm_breaker(**options: Any) -> CircuitBreaker:
    return CircuitBreaker(on=Exception, exclude=_NotCounted, **options)


def _through_breaker(
    breaker: CircuitBreaker, func: Callable[..., Any], counts: Callable[[BaseException], bool]
) -> Callable[..., Any]:
    """Call ``func`` through ``breaker``; only errors ``counts`` accepts open it."""
    if _core.is_async(func):

        async def ainner(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                if counts(exc):
                    raise
                raise _NotCounted(exc) from None

        @functools.wraps(func)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await breaker.call(ainner, *args, **kwargs)
            except _NotCounted as carrier:
                error = carrier.error
            raise error

        setattr(async_call, _LAYER_ATTR, ("breaker", breaker))
        return async_call

    def inner(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if counts(exc):
                raise
            raise _NotCounted(exc) from None

    @functools.wraps(func)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return breaker.call(inner, *args, **kwargs)
        except _NotCounted as carrier:
            error = carrier.error
        raise error

    setattr(call, _LAYER_ATTR, ("breaker", breaker))
    return call


class _BreakerLayer:
    """A policy ``breaker=`` layer whose breaker counts only `llm_transient` errors."""

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker
        setattr(self, _LAYER_ATTR, ("breaker", breaker))

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        return cast("Callable[P, R]", _through_breaker(self.breaker, func, llm_transient))


# ----------------------------------------------------------------------------- usage


@dataclasses.dataclass(frozen=True)
class Usage:
    """Tokens one LLM call used.

    Attributes:
        input_tokens: Prompt tokens (including cache reads and writes, when reported).
        output_tokens: Completion tokens.
        model: The model that served the call, when the response names it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens


def _count(raw: Any, *names: str) -> int | None:
    for name in names:
        value = _int(_field(raw, name))
        if value is not None:
            return max(value, 0)
    return None


def read_usage(response: Any) -> Usage | None:
    """Read token usage from a provider response without importing its SDK.

    Understands ``response.usage`` with ``input_tokens`` / ``output_tokens``
    (Anthropic style, plus ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens``, added to the input count) or ``prompt_tokens`` /
    ``completion_tokens`` (OpenAI style), as attributes or dict keys, and a
    ``model`` field next to it. A `Usage` is returned as is.

    Args:
        response: A response object or dict.

    Returns:
        The `Usage`, or None when the response reports none.

    Example:
        ```python
        from python_wrap.llm import read_usage

        usage = read_usage({"model": "m-1", "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        assert usage is not None and usage.total_tokens == 15 and usage.model == "m-1"
        ```
    """
    if isinstance(response, Usage):
        return response
    raw = _field(response, "usage")
    if raw is None:
        return None
    inputs = _count(raw, "input_tokens", "prompt_tokens")
    outputs = _count(raw, "output_tokens", "completion_tokens")
    if inputs is None and outputs is None:
        return None
    cached = (_count(raw, "cache_creation_input_tokens") or 0) + (
        _count(raw, "cache_read_input_tokens") or 0
    )
    model = _field(response, "model")
    return Usage(
        (inputs or 0) + cached, outputs or 0, model if isinstance(model, str) and model else None
    )


UsageReader = Callable[[Any], Any]


def _coerce_usage(value: Any) -> Usage | None:
    if value is None or isinstance(value, Usage):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return Usage(max(int(value[0]), 0), max(int(value[1]), 0))
    if isinstance(value, Mapping) and "usage" not in value:
        return read_usage({"usage": value, "model": value.get("model")})
    return read_usage(value)


def _usage_of(result: Any, reader: UsageReader | None) -> Usage | None:
    return _coerce_usage(reader(result)) if reader is not None else read_usage(result)


Prices = Mapping[str, tuple[float, float]]


def _price(prices: Prices, model: str | None) -> tuple[float, float] | None:
    if model is not None:
        if model in prices:
            return prices[model]
        matches = [name for name in prices if name != "*" and model.startswith(name)]
        if matches:
            return prices[max(matches, key=len)]
    return prices.get("*")


def _cost(usage: Usage, prices: Prices | None, default_model: str | None) -> float:
    if not prices:
        return 0.0
    model = usage.model or default_model
    price = _price(prices, model)
    if price is None:
        raise LookupError(
            f"no price for model {model!r}: add it (or a '*' default) to prices=, "
            "otherwise its calls would not count against the dollar budget"
        )
    return (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1_000_000


# ---------------------------------------------------------------------------- budget


@dataclasses.dataclass(frozen=True)
class Spend:
    """Tokens and dollars spent in one budget scope."""

    tokens: int = 0
    usd: float = 0.0


class BudgetBackend(Protocol):
    """Where a `Budget` keeps its counters; implement it to share them between processes.

    Keys are strings. ``add`` must be atomic and return the new totals; ``ttl`` (seconds
    or None) bounds how long a counter needs to live (budgets with ``period=`` pass it).
    """

    def add(self, key: str, tokens: int, usd: float, ttl: float | None, /) -> tuple[int, float]:
        """Add to a counter and return its new ``(tokens, usd)``."""

    def get(self, key: str, /) -> tuple[int, float]:
        """Return the counter's ``(tokens, usd)``; ``(0, 0.0)`` when unknown."""

    def delete(self, key: str, /) -> None:
        """Forget a counter."""


class _LocalBackend:
    """Thread-safe in-process counters, with expiry for windowed budgets."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float, float | None]] = {}
        self._lock = _fork.ForkSafeLock()
        self._adds = 0

    def _live(self, key: str, now: float) -> tuple[int, float]:
        entry = self._data.get(key)
        if entry is None or (entry[2] is not None and now >= entry[2]):
            return 0, 0.0
        return entry[0], entry[1]

    def add(self, key: str, tokens: int, usd: float, ttl: float | None) -> tuple[int, float]:
        now = _core.wall_time()
        with self._lock:
            used_tokens, used_usd = self._live(key, now)
            old = self._data.get(key)
            expires = old[2] if old is not None and (old[2] is None or now < old[2]) else None
            if expires is None and ttl is not None:
                expires = now + ttl
            self._data[key] = (used_tokens + tokens, used_usd + usd, expires)
            self._adds += 1
            if self._adds % 256 == 0:
                for name in [k for k, v in self._data.items() if v[2] is not None and now >= v[2]]:
                    del self._data[name]
            return used_tokens + tokens, used_usd + usd

    def get(self, key: str) -> tuple[int, float]:
        with self._lock:
            return self._live(key, _core.wall_time())

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_REDIS_ADD = """
local t=redis.call('HINCRBY',KEYS[1],'tokens',ARGV[1])
local u=redis.call('HINCRBYFLOAT',KEYS[1],'usd',ARGV[2])
if tonumber(ARGV[3])>0 and redis.call('PTTL',KEYS[1])<0 then
 redis.call('PEXPIRE',KEYS[1],ARGV[3]) end
return {t, tostring(u)}
"""


class RedisBudgetBackend:
    """A `BudgetBackend` on Redis, so every worker process shares one budget.

    Takes any synchronous ``redis.Redis``-compatible client (``eval``, ``hmget``,
    ``delete``); redis is never imported. Each counter is one hash updated by a Lua
    script, so concurrent adds never lose spend. Async budgets run each round trip in a
    worker thread.

    Args:
        client: The Redis client.
        prefix: Namespace for the keys.
    """

    def __init__(self, client: Any, prefix: str = "python-wrap:budget") -> None:
        if not prefix:
            raise ValueError("RedisBudgetBackend needs a nonempty prefix")
        self.client, self.prefix = client, prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def add(self, key: str, tokens: int, usd: float, ttl: float | None) -> tuple[int, float]:
        millis = 0 if ttl is None else max(1, math.ceil(ttl * 1000))
        result = self.client.eval(_REDIS_ADD, 1, self._key(key), tokens, repr(usd), millis)
        return int(result[0]), float(result[1])

    def get(self, key: str) -> tuple[int, float]:
        tokens, usd = self.client.hmget(self._key(key), "tokens", "usd")
        return int(tokens or 0), float(usd or 0.0)

    def delete(self, key: str) -> None:
        self.client.delete(self._key(key))


Scope = Literal["total", "key", "run", "chain"]
_SCOPES = ("total", "key", "run", "chain")
_NO_RUNS: Mapping[str, str] = MappingProxyType({})
_RUNS: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "python_wrap_llm_runs",
    default=_NO_RUNS,
)


class _RunScope:
    """``with budget.run():`` / ``async with budget.run():`` opens a fresh run."""

    def __init__(self, owner: Budget) -> None:
        self._owner = owner
        self._token: contextvars.Token[Mapping[str, str]] | None = None
        self._run = ""

    def __enter__(self) -> _RunScope:
        self._token, self._run = self._owner._open_run()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            self._owner._close_run(self._token, self._run)
            self._token = None

    async def __aenter__(self) -> _RunScope:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    @property
    def spent(self) -> Spend:
        """What this run has spent so far."""
        return self._owner._read(self._owner._qualify(f"run:{self._run}"))


class Budget:
    """A token and/or dollar limit for LLM calls; use it as a decorator.

    Before each call the scope's counter is checked: if it is already at or over a
    limit the call is not made and `BudgetExceeded` is raised. After a successful call
    the response's usage (see `read_usage`) is added, priced with ``prices``. The call
    that crosses a limit still returns its (already paid for) result; the next call is
    refused. Errors spend nothing.

    Scopes (``per=``):

    - ``"total"``: one counter for the budget's lifetime (or each ``period``).
    - ``"key"``: one counter per ``key(*args, **kwargs)``, for example per user.
    - ``"run"``: one counter per ``with budget.run():`` block (sync or async; context
      variables carry it into tasks and `hedge`/`run_in_thread` threads). Calling outside
      a run raises `RuntimeError`.
    - ``"chain"``: like ``"run"``, but the outermost decorated call opens the run, so an
      agent loop decorated with the same budget caps everything it calls.

    For streamed responses, call `record` with the final usage yourself.

    Args:
        tokens: Token limit (input plus output), or None.
        usd: Dollar limit, or None (then ``prices`` is optional).
        per: The scope, see above.
        key: For ``per="key"``: function of the call arguments returning the key.
        period: Reset the counters every ``period`` seconds (windows aligned to the wall
            clock, e.g. ``86400`` for a daily budget); None never resets.
        prices: ``{"model": (usd_per_million_input, usd_per_million_output)}``. The
            longest key the model name starts with wins, ``"*"`` is the default. With a
            ``usd`` limit, a model without a price raises `LookupError` after the call.
        model: The model to price responses that do not name one.
        usage: ``usage(response)`` returning a `Usage`, ``(input, output)`` or a usage
            mapping, for responses `read_usage` does not understand.
        name: Used in errors, events and backend keys.
        backend: A `BudgetBackend` (e.g. `RedisBudgetBackend`) to share counters
            between processes; in-process counters otherwise.

    Raises:
        ValueError: For a missing or out-of-range limit, unknown scope, or a ``per="key"``
            budget without ``key=``.

    Example:
        ```python
        from python_wrap.llm import Budget, BudgetExceeded

        spend = Budget(usd=0.01, prices={"m-1": (1.0, 4.0)})

        @spend
        def complete(prompt: str) -> dict[str, object]:
            return {"model": "m-1", "usage": {"input_tokens": 2000, "output_tokens": 1000}}

        complete("one")  # $0.006
        complete("two")  # $0.012: crosses the limit, result still returned
        try:
            complete("three")
        except BudgetExceeded as exc:
            assert exc.scope == "total"
        assert spend.spent().tokens == 6000
        ```
    """

    def __reduce__(self) -> Any:
        _core.refuse_pickle(self)

    def __init__(
        self,
        *,
        tokens: int | None = None,
        usd: float | None = None,
        per: Scope = "total",
        key: Callable[..., Hashable] | None = None,
        period: float | None = None,
        prices: Prices | None = None,
        model: str | None = None,
        usage: UsageReader | None = None,
        name: str = "budget",
        backend: BudgetBackend | None = None,
    ) -> None:
        if tokens is None and usd is None:
            raise ValueError("a Budget needs tokens= or usd= (or both)")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int)):
            raise ValueError(f"tokens must be an integer, got {tokens!r}")
        if tokens is not None and tokens < 1:
            raise ValueError(f"tokens must be >= 1, got {tokens!r}")
        if usd is not None and (not math.isfinite(usd) or usd <= 0):
            raise ValueError(f"usd must be a positive number, got {usd!r}")
        if per not in _SCOPES:
            raise ValueError(f"per must be one of {', '.join(_SCOPES)}; got {per!r}")
        if per == "key" and key is None:
            raise ValueError('per="key" needs key=, a function of the call arguments')
        if period is not None and (not math.isfinite(period) or period <= 0):
            raise ValueError(f"period must be positive, got {period!r}")
        if usd is not None and not prices:
            raise ValueError("a usd= limit needs prices= to turn tokens into dollars")
        if not name:
            raise ValueError("name must not be empty")
        self.tokens, self.usd, self.per, self.key = tokens, usd, per, key
        self.period, self.prices, self.model, self.usage = period, prices, model, usage
        self.name = name
        self.backend: BudgetBackend = backend if backend is not None else _LocalBackend()
        self._shared = backend is not None
        self._id = uuid.uuid4().hex
        setattr(self, _LAYER_ATTR, ("budget", self._settings()))

    def _settings(self) -> dict[str, Any]:
        return {
            "llm": "budget",
            "name": self.name,
            "tokens": self.tokens,
            "usd": self.usd,
            "per": self.per,
            "shared": self._shared,
        }

    # -- scopes ------------------------------------------------------------------
    def _qualify(self, base: str) -> str:
        if self.period is not None:
            base += f":w{int(_core.wall_time() // self.period)}"
        return f"{self.name}:{base}"

    def _scope_key(self, args: tuple[Any, ...], kwargs: dict[str, Any], key: Any) -> str:
        if self.per == "total":
            return self._qualify("total")
        if self.per == "key":
            if key is _MISSING:
                assert self.key is not None
                key = self.key(*args, **kwargs)
            return self._qualify(f"key:{digest(key)}")
        run = _RUNS.get().get(self._id)
        if run is None:
            raise RuntimeError(
                f"budget {self.name!r} is per={self.per!r} but no run is active: "
                "wrap the work in `with budget.run():`"
            )
        return self._qualify(f"run:{run}")

    def _open_run(self) -> tuple[contextvars.Token[Mapping[str, str]], str]:
        run = uuid.uuid4().hex
        return _RUNS.set({**_RUNS.get(), self._id: run}), run

    def _close_run(self, token: contextvars.Token[Mapping[str, str]], run: str) -> None:
        _RUNS.reset(token)
        self.backend.delete(self._qualify(f"run:{run}"))

    def run(self) -> _RunScope:
        """Open a new run scope: ``with budget.run():`` or ``async with budget.run():``.

        Every decorated call inside shares one fresh counter, deleted when the block
        exits. ``.spent`` on the returned object reports the run's spend.
        """
        return _RunScope(self)

    # -- accounting --------------------------------------------------------------
    def _read(self, scope: str) -> Spend:
        tokens, usd = self.backend.get(scope)
        return Spend(tokens, usd)

    def _over(self, spend: Spend) -> bool:
        return (self.tokens is not None and spend.tokens >= self.tokens) or (
            self.usd is not None and spend.usd >= self.usd
        )

    def _refuse(self, function: str, spend: Spend) -> BudgetExceeded:
        _core.emit(
            "llm.budget_exceeded",
            function,
            budget=self.name,
            scope=self.per,
            tokens=spend.tokens,
            usd=spend.usd,
        )
        limits = []
        if self.tokens is not None:
            limits.append(f"{spend.tokens}/{self.tokens} tokens")
        if self.usd is not None:
            limits.append(f"${spend.usd:.4f}/${self.usd:g}")
        return BudgetExceeded(
            f"budget {self.name!r} ({self.per}) is used up: {', '.join(limits)}",
            budget=self.name,
            scope=self.per,
            tokens=spend.tokens,
            usd=spend.usd,
            limit_tokens=self.tokens,
            limit_usd=self.usd,
        )

    def _check(self, scope: str, function: str) -> None:
        spend = self._read(scope)
        if self._over(spend):
            raise self._refuse(function, spend)

    def _add(self, scope: str, usage: Usage | None) -> Spend:
        if usage is None:
            return self._read(scope)
        usd = _cost(usage, self.prices, self.model) if self.usd is not None or self.prices else 0.0
        tokens, total_usd = self.backend.add(scope, usage.total_tokens, usd, self.period)
        return Spend(tokens, total_usd)

    def record(self, response: Any, *, key: Any = None) -> Spend:
        """Add a response's (or a `Usage`'s) tokens to the current scope by hand.

        Use it for streamed responses, whose usage arrives with the last chunk.

        Args:
            response: A response, a usage mapping, a `Usage` or ``(input, output)``.
            key: The key, for ``per="key"`` budgets.

        Returns:
            The scope's spend after adding.

        Raises:
            RuntimeError: For a run/chain budget outside a run.
            ValueError: For a ``per="key"`` budget without ``key=``.
        """
        if self.per == "key" and key is None:
            raise ValueError('record() on a per="key" budget needs key=')
        usage = _usage_of(response, self.usage) if self.usage else _coerce_usage(response)
        return self._add(self._scope_key((), {}, key if self.per == "key" else _MISSING), usage)

    def spent(self, *, key: Any = None) -> Spend:
        """Spend in the current scope (for ``per="key"``, of ``key``)."""
        if self.per == "key" and key is None:
            raise ValueError('spent() on a per="key" budget needs key=')
        return self._read(self._scope_key((), {}, key if self.per == "key" else _MISSING))

    def exhausted(self, *, key: Any = None) -> bool:
        """True when the next call in the current scope would be refused."""
        return self._over(self.spent(key=key))

    def reset(self, *, key: Any = None) -> None:
        """Forget the current scope's spend (for ``per="key"``, of ``key``)."""
        if self.per == "key" and key is None:
            raise ValueError('reset() on a per="key" budget needs key=')
        self.backend.delete(self._scope_key((), {}, key if self.per == "key" else _MISSING))

    # -- decorator ---------------------------------------------------------------
    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Decorate a function making LLM calls (sync or async)."""
        label = _core.qualname(func)
        chain = self.per == "chain"
        shared = self._shared

        if _core.is_async(func):
            afunc = cast("Callable[..., Awaitable[Any]]", func)

            async def offload(call: Callable[..., T], *args: Any) -> T:
                return await asyncio.to_thread(call, *args) if shared else call(*args)

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                opened = self._open_run() if chain and self._id not in _RUNS.get() else None
                try:
                    scope = self._scope_key(args, kwargs, _MISSING)
                    await offload(self._check, scope, label)
                    result = await afunc(*args, **kwargs)
                    await offload(self._add, scope, _usage_of(result, self.usage))
                    return result
                finally:
                    if opened is not None:
                        await offload(self._close_run, *opened)

            setattr(async_wrapper, _LAYER_ATTR, ("budget", self._settings()))
            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            opened = self._open_run() if chain and self._id not in _RUNS.get() else None
            try:
                scope = self._scope_key(args, kwargs, _MISSING)
                self._check(scope, label)
                result = func(*args, **kwargs)
                self._add(scope, _usage_of(result, self.usage))
                return result
            finally:
                if opened is not None:
                    self._close_run(*opened)

        setattr(wrapper, _LAYER_ATTR, ("budget", self._settings()))
        return wrapper

    def __repr__(self) -> str:
        limits = [f"tokens={self.tokens}"] if self.tokens is not None else []
        limits += [f"usd={self.usd:g}"] if self.usd is not None else []
        return f"Budget({self.name!r}, {', '.join(limits)}, per={self.per!r})"


_MISSING: Any = object()


def budget(
    *,
    tokens: int | None = None,
    usd: float | None = None,
    per: Scope = "total",
    key: Callable[..., Hashable] | None = None,
    period: float | None = None,
    prices: Prices | None = None,
    model: str | None = None,
    usage: UsageReader | None = None,
    name: str = "budget",
    backend: BudgetBackend | None = None,
) -> Budget:
    """Build a `Budget`; ``@budget(tokens=50_000, per="key", key=...)`` decorates directly.

    Keep the returned object when you want to inspect or reset it (``spend =
    budget(...)``, then ``@spend``). See `Budget` for every argument.

    Example:
        ```python
        from python_wrap.llm import BudgetExceeded, budget

        @budget(tokens=100, per="key", key=lambda user, prompt: user)
        def ask(user: str, prompt: str) -> dict[str, object]:
            return {"usage": {"input_tokens": 60, "output_tokens": 40}}

        ask("ann", "hi")
        ask("bob", "hi")  # bob has his own allowance
        try:
            ask("ann", "again")
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("ann is over budget")
        ```
    """
    return Budget(
        tokens=tokens,
        usd=usd,
        per=per,
        key=key,
        period=period,
        prices=prices,
        model=model,
        usage=usage,
        name=name,
        backend=backend,
    )


# ------------------------------------------------------------------ token rate limit


def _text_len(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_text_len(value.get(name)) for name in ("content", "text"))
    if isinstance(value, (list, tuple)):
        return sum(_text_len(item) for item in value)
    content = getattr(value, "content", None)
    return _text_len(content) if content is not None else 0


def estimate_tokens(*args: Any, **kwargs: Any) -> int:
    """A dependency-free token estimate for a request: about four characters per token.

    Counts the text in ``messages``, ``prompt``, ``input`` and ``system`` keyword
    arguments and positional strings, plus the requested output allowance
    (``max_tokens``, ``max_output_tokens`` or ``max_completion_tokens``). Pass your own
    ``estimate=`` (a real tokenizer) to `token_rate_limit` when precision matters.

    Returns:
        The estimated total tokens (at least 1).
    """
    chars = sum(_text_len(kwargs.get(name)) for name in ("messages", "prompt", "input", "system"))
    chars += sum(_text_len(arg) for arg in args if isinstance(arg, (str, list, tuple)))
    output = 0
    for name in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
        value = _int(kwargs.get(name))
        if value is not None:
            output = max(value, 0)
            break
    return max(1, math.ceil(chars / 4) + output)


class TokenRateLimiter:
    """A tokens-per-minute limit for LLM calls; build it with `token_rate_limit`.

    A token bucket holding up to ``tokens_per_minute`` tokens, refilled continuously.
    Each call reserves its estimate before it starts; when the call returns, the
    reservation is replaced by the real usage (refunding or charging the difference),
    and a failed call is refunded. A request larger than the whole bucket is admitted
    when the bucket is full. Reads time through ``python_wrap._core``, so
    `python_wrap.testing.fake_clock` controls it; ``disable_rate_limits()`` bypasses it.

    Attributes:
        tokens_per_minute: The limit.
        name: Used in events and errors.
    """

    def __reduce__(self) -> Any:
        _core.refuse_pickle(self)

    def __init__(
        self,
        tokens_per_minute: int,
        *,
        estimate: Callable[..., int] | int | None = None,
        usage: UsageReader | None = None,
        block: bool = True,
        max_wait: float | None = None,
        name: str = "tokens",
        store: Any = None,
    ) -> None:
        if (
            isinstance(tokens_per_minute, bool)
            or not isinstance(tokens_per_minute, int)
            or tokens_per_minute < 1
        ):
            raise ValueError(
                f"tokens_per_minute must be an integer >= 1, got {tokens_per_minute!r}"
            )
        if max_wait is not None and (not math.isfinite(max_wait) or max_wait < 0):
            raise ValueError(f"max_wait must be >= 0, got {max_wait!r}")
        if isinstance(estimate, int) and (isinstance(estimate, bool) or estimate < 0):
            raise ValueError(f"estimate must be >= 0, got {estimate!r}")
        self.tokens_per_minute = tokens_per_minute
        self.name = name
        self.block, self.max_wait, self.usage = block, max_wait, usage
        fixed = estimate if isinstance(estimate, int) else None
        self._estimate: Callable[..., int] = (
            (lambda *a, **k: fixed)
            if fixed is not None
            else (estimate if callable(estimate) else estimate_tokens)
        )
        self._capacity = float(tokens_per_minute)
        self._rate = tokens_per_minute / 60.0
        self._level = self._capacity
        self._stamp: float | None = None
        self._blocked = 0.0  # monotonic time until which a provider asked us to stop
        self._store = store
        self._lock = _fork.ForkSafeLock()
        setattr(
            self,
            _LAYER_ATTR,
            (
                "limiter",
                {
                    "llm": "token_limit",
                    "name": name,
                    "tokens_per_minute": tokens_per_minute,
                    "shared": store is not None,
                },
            ),
        )

    def _refill(self) -> None:
        now = _core.monotonic()
        if self._stamp is not None:
            elapsed = max(now - self._stamp, 0.0)  # a clock swapped in tests may step back
            self._level = min(self._capacity, self._level + elapsed * self._rate)
        self._stamp = now

    @property
    def available(self) -> float:
        """Tokens that could be reserved right now (negative while in debt)."""
        if self._store is not None:
            return float(self._store.level(self._capacity, self._rate))
        with self._lock:
            self._refill()
            return self._level

    def try_reserve(self, tokens: int) -> float:
        """Reserve ``tokens`` if possible: 0.0 when reserved, else seconds to wait."""
        if self._store is not None:
            return float(self._store.reserve(tokens, self._capacity, self._rate))
        with self._lock:
            self._refill()
            if self._blocked > _core.monotonic():
                return self._blocked - _core.monotonic()
            need = min(float(tokens), self._capacity)
            if self._level + 1e-6 >= need:  # waiting exactly retry_after admits (float slack)
                self._level -= tokens
                return 0.0
            return (need - self._level) / self._rate

    def _force(self, tokens: int) -> None:
        if self._store is not None:
            self._store.force(tokens, self._capacity, self._rate)
            return
        with self._lock:
            self._refill()
            self._level -= tokens

    def settle(self, reserved: int, actual: int) -> None:
        """Replace a reservation of ``reserved`` tokens by the ``actual`` usage."""
        if self._store is not None:
            self._store.settle(reserved, actual, self._capacity, self._rate)
            return
        with self._lock:
            self._refill()
            self._level = min(self._capacity, self._level + reserved - actual)

    def _refused(self, wait: float, label: str) -> RateLimitExceeded:
        _core.emit("rate_limit.rejected", self.name, retry_after=wait)
        return RateLimitExceeded(
            f"token rate limit {self.name!r} ({self.tokens_per_minute}/min) exceeded for {label}",
            retry_after=wait,
        )

    def _waits(self, waited: float, wait: float) -> bool:
        return self.block and (self.max_wait is None or waited + wait <= self.max_wait)

    def acquire(self, tokens: int, *, label: str = "call") -> None:
        """Reserve ``tokens``, waiting (``block=True``) or raising `RateLimitExceeded`."""
        waited = 0.0
        while not _core.overrides.disable_rate_limits:
            wait = self.try_reserve(tokens)
            if wait <= 0:
                break
            if not self._waits(waited, wait):
                raise self._refused(wait, label)
            if _core.overrides.no_sleep:
                self._force(tokens)
                break
            _core.sleep(wait)
            waited += wait
            _core.emit("rate_limit.waited", self.name, seconds=wait)

    async def aacquire(self, tokens: int, *, label: str = "call") -> None:
        """Async `acquire`: waits with ``asyncio`` instead of blocking the thread."""
        waited = 0.0
        while not _core.overrides.disable_rate_limits:
            wait = await self._areserve(tokens)
            if wait <= 0:
                break
            if not self._waits(waited, wait):
                raise self._refused(wait, label)
            if _core.overrides.no_sleep:
                await self._aforce(tokens)
                await asyncio.sleep(0)
                break
            await _core.async_sleep(wait)
            waited += wait
            _core.emit("rate_limit.waited", self.name, seconds=wait)

    async def _areserve(self, tokens: int) -> float:
        store = self._store
        if store is None:
            return self.try_reserve(tokens)
        if hasattr(store, "areserve"):
            return float(await store.areserve(tokens, self._capacity, self._rate))
        return await asyncio.to_thread(self.try_reserve, tokens)

    async def _aforce(self, tokens: int) -> None:
        store = self._store
        if store is not None and hasattr(store, "aforce"):
            await store.aforce(tokens, self._capacity, self._rate)
        elif store is not None:
            await asyncio.to_thread(self._force, tokens)
        else:
            self._force(tokens)

    async def _asettle(self, reserved: int, actual: int) -> None:
        store = self._store
        if store is not None and hasattr(store, "asettle"):
            await store.asettle(reserved, actual, self._capacity, self._rate)
        elif store is not None:
            await asyncio.to_thread(self.settle, reserved, actual)
        else:
            self.settle(reserved, actual)

    def observe_headers(self, headers: Any) -> Any:
        """Update the bucket from a provider's rate-limit headers (sync).

        The provider's ``remaining`` token count lowers the bucket (never raises it), and
        ``Retry-After`` (or the reset time when nothing remains) blocks further requests
        until then. With a shared ``store=`` the block applies to every worker. See
        `python_wrap.llm_shared.parse_rate_limit_headers` for the headers understood.

        Returns:
            The parsed `python_wrap.llm_shared.RateLimitHint`.
        """
        from python_wrap.llm_shared import parse_rate_limit_headers

        if self._store is not None:
            return self._store.observe_headers(headers, capacity=self._capacity, rate=self._rate)
        hint = parse_rate_limit_headers(headers)
        block = hint.block_seconds
        with self._lock:
            self._refill()
            if hint.remaining_tokens is not None:
                self._level = min(self._level, hint.remaining_tokens)
            if block is not None and block > 0:
                self._blocked = max(self._blocked, _core.monotonic() + block)
        return hint

    async def aobserve_headers(self, headers: Any) -> Any:
        """Async `observe_headers`; shared stores are updated without blocking the loop."""
        store = self._store
        if store is not None and hasattr(store, "aobserve_headers"):
            return await store.aobserve_headers(headers, capacity=self._capacity, rate=self._rate)
        return await asyncio.to_thread(self.observe_headers, headers)

    @staticmethod
    def _error_headers(exc: BaseException) -> Any:
        headers = _field(exc, "headers")
        if headers is None:
            headers = _field(_field(exc, "response"), "headers")
        return headers

    def _learn(self, exc: BaseException) -> None:
        """Feed an error's response headers (a 429's, usually) to `observe_headers`."""
        headers = self._error_headers(exc)
        if headers is not None:
            with contextlib.suppress(Exception):  # a broken store must not mask the real error
                self.observe_headers(headers)

    async def _alearn(self, exc: BaseException) -> None:
        headers = self._error_headers(exc)
        if headers is not None:
            with contextlib.suppress(Exception):
                await self.aobserve_headers(headers)

    def _actual(self, result: Any, reserved: int) -> int:
        usage = _usage_of(result, self.usage)
        return reserved if usage is None else usage.total_tokens

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Decorate a sync or async function making LLM calls."""
        label = _core.qualname(func)

        if _core.is_async(func):
            afunc = cast("Callable[..., Awaitable[Any]]", func)

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _core.overrides.disable_rate_limits:
                    return await afunc(*args, **kwargs)
                cost = max(0, int(self._estimate(*args, **kwargs)))
                await self.aacquire(cost, label=label)
                try:
                    result = await afunc(*args, **kwargs)
                except BaseException as exc:
                    await self._asettle(cost, 0)
                    await self._alearn(exc)
                    raise
                await self._asettle(cost, self._actual(result, cost))
                return result

            setattr(async_wrapper, _LAYER_ATTR, getattr(self, _LAYER_ATTR))
            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if _core.overrides.disable_rate_limits:
                return func(*args, **kwargs)
            cost = max(0, int(self._estimate(*args, **kwargs)))
            self.acquire(cost, label=label)
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                self.settle(cost, 0)
                self._learn(exc)
                raise
            self.settle(cost, self._actual(result, cost))
            return result

        setattr(wrapper, _LAYER_ATTR, getattr(self, _LAYER_ATTR))
        return wrapper

    def __repr__(self) -> str:
        return f"TokenRateLimiter({self.name!r}, {self.tokens_per_minute}/min)"


def token_rate_limit(
    tokens_per_minute: int,
    *,
    estimate: Callable[..., int] | int | None = None,
    usage: UsageReader | None = None,
    block: bool = True,
    max_wait: float | None = None,
    name: str = "tokens",
    store: Any = None,
) -> TokenRateLimiter:
    """Limit LLM calls to ``tokens_per_minute``, reserving estimates up front.

    Args:
        tokens_per_minute: Bucket size and refill per minute.
        estimate: ``estimate(*args, **kwargs) -> int`` tokens for a call, or a fixed
            number; `estimate_tokens` (about four characters per token plus
            ``max_tokens``) by default.
        usage: Reads the real usage from a result when `read_usage` cannot; without
            any usage the estimate stands.
        block: Wait for tokens (emitting ``rate_limit.waited``); False raises
            `RateLimitExceeded` (with ``retry_after``) instead.
        max_wait: With ``block=True``, the longest total wait before raising.
        name: Used in events and errors.
        store: A shared window (`python_wrap.llm_shared.SQLiteTokenWindow`,
            `RedisTokenWindow`, `AsyncRedisTokenWindow`) so every worker process draws
            from one bucket and backs off together. Every worker must use the same
            ``tokens_per_minute``. Errors that carry rate-limit headers update it (see
            `TokenRateLimiter.observe_headers`).

    Returns:
        A `TokenRateLimiter`, usable as a decorator (one instance can guard several
        functions that share a provider quota).

    Example:
        ```python
        from python_wrap.errors import RateLimitExceeded
        from python_wrap.llm import token_rate_limit

        limit = token_rate_limit(1000, estimate=600, block=False)

        @limit
        def complete(prompt: str) -> dict[str, object]:
            return {"usage": {"input_tokens": 100, "output_tokens": 100}}

        complete("a")  # reserves 600, settles at the real 200
        complete("b")
        try:
            complete("c")
        except RateLimitExceeded as exc:
            assert exc.retry_after > 0
        ```
    """
    return TokenRateLimiter(
        tokens_per_minute,
        estimate=estimate,
        usage=usage,
        block=block,
        max_wait=max_wait,
        name=name,
        store=store,
    )


# ------------------------------------------------------------- request keys (cache)


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON-compatible, order-independent view of a request value."""
    if depth > 32:
        return repr(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v, depth + 1) for v in value), key=repr)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _plain(dump(), depth + 1)
        except Exception:
            return repr(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value), depth + 1)
    return repr(value)


def _hash(value: Any) -> str:
    text = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _request(
    signature: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    if signature is None:
        return {"args": list(args), **kwargs}
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    request: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        kind = signature.parameters[name].kind
        if kind is inspect.Parameter.VAR_KEYWORD:
            request.update(value)
        elif name not in ("self", "cls"):
            request[name] = value
    return request


def _signature(func: Callable[..., Any]) -> inspect.Signature | None:
    try:
        return inspect.signature(func)
    except (TypeError, ValueError):
        return None


_PROMPT_FIELDS = ("messages", "prompt", "input")


def _prompt_text(request: Mapping[str, Any]) -> str:
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Mapping):
            for name in ("content", "text"):
                walk(value.get(name))
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif getattr(value, "content", None) is not None:
            walk(value.content)

    for name in _PROMPT_FIELDS:
        walk(request.get(name))
    return "\n".join(parts)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else -1.0


class _LRU:
    """The default `llm_cache` store: a thread-safe LRU dict."""

    def __init__(self, maxsize: int | None) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = _fork.ForkSafeLock()

    def get(self, key: Hashable, /) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                self._data.move_to_end(key)
            return entry

    def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        with self._lock:
            self._data[key] = entry
            self._data.move_to_end(key)
            while self.maxsize is not None and len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def delete(self, key: Hashable, /) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _Index:
    """Recent prompt embeddings for ``similarity=`` lookups (in process, bounded)."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._rows: OrderedDict[str, tuple[str, tuple[float, ...]]] = OrderedDict()
        self._lock = _fork.ForkSafeLock()

    def add(self, key: str, group: str, vector: Sequence[float]) -> None:
        with self._lock:
            self._rows[key] = (group, tuple(float(x) for x in vector))
            self._rows.move_to_end(key)
            while len(self._rows) > self.maxsize:
                self._rows.popitem(last=False)

    def best(self, group: str, vector: Sequence[float], threshold: float) -> str | None:
        with self._lock:
            rows = list(self._rows.items())
        found, score = None, threshold
        for key, (row_group, row) in rows:
            if row_group == group:
                similarity = _cosine(vector, row)
                if similarity >= score:
                    found, score = key, similarity
        return found

    def discard(self, key: str) -> None:
        with self._lock:
            self._rows.pop(key, None)


_DEFAULT_IGNORE = ("stream", "timeout", "extra_headers", "user", "metadata", "request_id")


def llm_cache(
    *,
    ttl: float | None = 3600.0,
    store: Any = None,
    maxsize: int = 1024,
    key: Callable[..., Hashable] | None = None,
    ignore: Sequence[str] = _DEFAULT_IGNORE,
    similarity: Callable[[str], Sequence[float]] | None = None,
    threshold: float = 0.95,
) -> _Decorator[..., Any]:
    """Cache LLM responses keyed on the request: model, messages or prompt, and params.

    The key is a SHA-256 of the call's bound arguments (``**kwargs`` flattened, dicts
    order-independent, pydantic models and dataclasses dumped), minus the names in
    ``ignore`` (transport options such as ``timeout`` or ``user``). Calls with
    ``stream=True`` bypass the cache, as do failures and ``None`` results, which are
    never stored. ``disable_caching()`` turns it off in tests. Emits ``cache.hit`` and
    ``cache.miss``.

    Semantic matching: pass ``similarity=embed``, a function turning the prompt text into
    a vector (your own embedding call; nothing is imported). On an exact miss, the
    prompt's vector is compared (cosine) with recent prompts that used the **same**
    model and parameters, and an entry at least ``threshold`` similar is served. The
    vectors are kept in process (the last ``maxsize``); the entries live in ``store``.

    Args:
        ttl: Seconds an entry stays valid (wall clock), or None for no expiry.
        store: A `CacheStore` (sync methods) or `AsyncCacheStore` (async functions only),
            e.g. ``RedisStore(client, "llm:")``; a thread-safe in-process LRU of
            ``maxsize`` entries by default.
        maxsize: Size of the default store and of the similarity index.
        key: ``key(*args, **kwargs)`` replaces the derived key.
        ignore: Argument names left out of the key.
        similarity: Optional embedding function for semantic matches.
        threshold: Minimum cosine similarity for a semantic match (0-1].

    Returns:
        A decorator for sync or async functions.

    Raises:
        ValueError: For a non-positive ``ttl``/``maxsize`` or a threshold outside (0, 1].

    Example:
        ```python
        from python_wrap.llm import llm_cache

        calls = []

        @llm_cache(ttl=600)
        def complete(*, model: str, messages: list[dict[str, str]], temperature: float = 0) -> str:
            calls.append(model)
            return "4"

        question = [{"role": "user", "content": "2+2?"}]
        assert complete(model="m-1", messages=question) == "4"
        assert complete(model="m-1", messages=question, temperature=0) == "4"
        assert calls == ["m-1"]  # the second call was served from the cache
        ```
    """
    if ttl is not None and (not math.isfinite(ttl) or ttl <= 0):
        raise ValueError(f"ttl must be positive or None, got {ttl!r}")
    if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
        raise ValueError(f"maxsize must be an integer >= 1, got {maxsize!r}")
    if not 0 < threshold <= 1:
        raise ValueError(f"threshold must be in (0, 1], got {threshold!r}")
    skip = frozenset(ignore)
    settings = {
        "llm": "cache",
        "ttl": ttl,
        "bounded": True,
        "self_in_key": False,
        "similarity": similarity is not None,
    }

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        backing = store if store is not None else _LRU(maxsize)
        index = _Index(maxsize) if similarity is not None else None
        signature = _signature(func)
        label = _core.qualname(func)
        prefix = f"llm:{func.__module__}.{label}:"

        def plan(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, str] | None:
            """``(key, group, prompt)`` for a cacheable call, None to bypass the cache."""
            if _core.overrides.disable_caching:
                return None
            try:
                request = _request(signature, args, kwargs)
            except TypeError:
                return None  # let the call raise its own argument error
            if request.get("stream") is True:
                return None
            request = {k: v for k, v in request.items() if k not in skip}
            exact = prefix + (digest(key(*args, **kwargs)) if key is not None else _hash(request))
            group = _hash({k: v for k, v in request.items() if k not in _PROMPT_FIELDS})
            return exact, group, _prompt_text(request)

        def fresh(entry: Any) -> bool:
            return (
                isinstance(entry, tuple)
                and len(entry) == 2
                and (entry[0] is None or _core.wall_time() < entry[0])
            )

        def entry_for(value: Any) -> tuple[float | None, Any]:
            return (None if ttl is None else _core.wall_time() + ttl, value)

        if _core.is_async(func):
            afunc = cast("Callable[..., Awaitable[Any]]", func)

            async def alookup(found: str) -> Any:
                entry = await _maybe_await(backing.get(found))
                return entry[1] if fresh(entry) else None

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                planned = plan(args, kwargs)
                if planned is None:
                    return await afunc(*args, **kwargs)
                exact, group, prompt = planned
                value = await alookup(exact)
                vector: Sequence[float] | None = None
                if value is None and index is not None and similarity is not None:
                    vector = cast("Sequence[float]", await _maybe_await(similarity(prompt)))
                    near = index.best(group, vector, threshold)
                    if near is not None:
                        value = await alookup(near)
                        if value is None:
                            index.discard(near)
                if value is not None:
                    _core.emit("cache.hit", label)
                    return value
                _core.emit("cache.miss", label)
                result = await afunc(*args, **kwargs)
                if result is not None:
                    await _maybe_await(backing.set(exact, entry_for(result), ttl))
                    if index is not None and vector is not None:
                        index.add(exact, group, vector)
                return result

            setattr(async_wrapper, _LAYER_ATTR, ("cache", settings))
            return cast("Callable[P, R]", async_wrapper)

        if inspect.iscoroutinefunction(getattr(backing, "get", None)):
            raise TypeError(
                f"{label} is synchronous but the store has async methods; use a CacheStore"
            )

        def lookup(found: str) -> Any:
            entry = backing.get(found)
            return entry[1] if fresh(entry) else None

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            planned = plan(args, kwargs)
            if planned is None:
                return func(*args, **kwargs)
            exact, group, prompt = planned
            value = lookup(exact)
            vector: Sequence[float] | None = None
            if value is None and index is not None and similarity is not None:
                vector = similarity(prompt)
                near = index.best(group, vector, threshold)
                if near is not None:
                    value = lookup(near)
                    if value is None:
                        index.discard(near)
            if value is not None:
                _core.emit("cache.hit", label)
                return cast(R, value)
            _core.emit("cache.miss", label)
            result = func(*args, **kwargs)
            if result is not None:
                backing.set(exact, entry_for(result), ttl)
                if index is not None and vector is not None:
                    index.add(exact, group, vector)
            return result

        setattr(wrapper, _LAYER_ATTR, ("cache", settings))
        return wrapper

    setattr(decorate, _LAYER_ATTR, ("cache", settings))
    return decorate


# ------------------------------------------------------------------- fallback chains


def _falls_back(exc: BaseException) -> bool:
    return isinstance(exc, CircuitOpenError) or llm_transient(exc)


def _predicate(
    on: ExceptionTypes | Callable[[BaseException], bool] | None,
) -> Callable[[BaseException], bool]:
    if on is None:
        return _falls_back
    if isinstance(on, (type, tuple)):
        types = on
        return lambda exc: isinstance(exc, types)
    return on


def _model_name(func: Callable[..., Any]) -> str:
    model = getattr(func, "model", None)
    return model if isinstance(model, str) and model else _core.qualname(func)


@overload
def fallback_models(
    models: Sequence[Callable[P, R]],
    /,
    *,
    names: Sequence[str] | None = ...,
    on: ExceptionTypes | Callable[[BaseException], bool] | None = ...,
    breaker: bool | Mapping[str, CircuitBreaker] = ...,
    cached_answer: bool | Any = ...,
    name: str | None = ...,
) -> Callable[P, R]: ...
@overload
def fallback_models(
    models: None = None,
    /,
    *,
    backups: Sequence[Callable[..., Any]],
    names: Sequence[str] | None = ...,
    on: ExceptionTypes | Callable[[BaseException], bool] | None = ...,
    breaker: bool | Mapping[str, CircuitBreaker] = ...,
    cached_answer: bool | Any = ...,
    name: str | None = ...,
) -> _Decorator[..., Any]: ...
def fallback_models(
    models: Sequence[Callable[..., Any]] | None = None,
    /,
    *,
    backups: Sequence[Callable[..., Any]] = (),
    names: Sequence[str] | None = None,
    on: ExceptionTypes | Callable[[BaseException], bool] | None = None,
    breaker: bool | Mapping[str, CircuitBreaker] = True,
    cached_answer: bool | Any = False,
    name: str | None = None,
) -> Any:
    """Try LLM calls in order until one succeeds: a model fallback chain.

    Two forms: ``fallback_models([call_a, call_b, call_c])`` returns the chain as one
    callable; ``@fallback_models(backups=[call_b, call_c])`` makes the decorated
    function the primary. All entries take the same arguments and are all sync or all
    ``async def``.

    Each entry sits behind its own `CircuitBreaker` (``breaker=True``: 5 transient
    failures open it for 30 s; refusals and bad requests are not counted), so a
    provider that is down is skipped at once instead of timing out on every call. Pass
    a mapping ``{name: CircuitBreaker}`` to supply your own (the same instance under two
    names shares one breaker per provider), or False for none.

    An error matching ``on`` moves on to the next entry; anything else is raised at
    once. By default that is `llm_transient` errors and `CircuitOpenError`: a
    content-policy refusal or a bad request is not hidden by another model's answer.
    With ``cached_answer=True`` (or a `CacheStore`), the last successful answer for the
    same arguments is remembered and served when every entry fails. Every served call
    emits ``llm.fallback_served`` naming the model.

    Args:
        models: The entries, primary first (list form).
        backups: The entries after the decorated function (decorator form).
        names: Model names for events and breakers; default each entry's ``model``
            attribute or qualified name.
        on: Exception type(s) or predicate that trigger the next entry.
        breaker: True, False, or a mapping of name to breaker.
        cached_answer: False, True (in-process LRU of 1024 answers) or a store.
        name: The chain's name in events and breaker names.

    Returns:
        The chain (list form) or a decorator (decorator form).

    Raises:
        ValueError: For an empty chain or a wrong number of names.
        TypeError: When sync and async entries are mixed.

    Example:
        ```python
        from python_wrap.events import listening
        from python_wrap.llm import fallback_models

        class Overloaded(Exception):
            status_code = 529

        def big_model(prompt: str) -> str:
            raise Overloaded

        def small_model(prompt: str) -> str:
            return f"small: {prompt}"

        ask = fallback_models([big_model, small_model], names=["big", "small"])
        events = []
        with listening(events.append):
            assert ask("hi") == "small: hi"
        assert events[-1].fields["model"] == "small"
        assert events[-1].fields["failed"] == ("big",)
        ```
    """
    if models is None:
        if not backups:
            raise ValueError("fallback_models() needs a list of models or backups=")
        rest = list(backups)

        def decorate(func: Callable[P, R]) -> Callable[P, R]:
            chain = _build_chain(
                [func, *rest], names, on=on, breaker=breaker, cached_answer=cached_answer, name=name
            )
            return cast("Callable[P, R]", chain)

        return decorate
    return _build_chain(
        list(models), names, on=on, breaker=breaker, cached_answer=cached_answer, name=name
    )


def _build_chain(
    models: list[Callable[..., Any]],
    names: Sequence[str] | None,
    *,
    on: ExceptionTypes | Callable[[BaseException], bool] | None,
    breaker: bool | Mapping[str, CircuitBreaker],
    cached_answer: bool | Any,
    name: str | None,
) -> Callable[..., Any]:
    if not models:
        raise ValueError("fallback_models() needs at least one model")
    labels = list(names) if names is not None else [_model_name(m) for m in models]
    if len(labels) != len(models):
        raise ValueError(f"got {len(labels)} names for {len(models)} models")
    kinds = {_core.is_async(m) for m in models}
    if len(kinds) > 1:
        raise TypeError("fallback_models() entries must be all sync or all async def")
    is_async = kinds == {True}
    chain_name = name or _core.qualname(models[0])
    falls_back = _predicate(on)

    entries: list[tuple[str, Callable[..., Any]]] = []
    for label, model in zip(labels, models, strict=True):
        if breaker is True:
            guard = _llm_breaker(
                failure_threshold=5, recovery_timeout=30.0, name=f"{chain_name}:{label}"
            )
            entries.append((label, _through_breaker(guard, model, llm_transient)))
        elif isinstance(breaker, Mapping) and label in breaker:
            given = breaker[label]
            entries.append((label, functools.partial(given.call, model)))
        else:
            entries.append((label, model))

    store: Any = None
    if cached_answer is True:
        store = _LRU(1024)
    elif cached_answer not in (False, None):
        store = cached_answer
    signature = _signature(models[0])

    def answer_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        try:
            return f"llm-fallback:{chain_name}:{_hash(_request(signature, args, kwargs))}"
        except TypeError:
            return None

    def served(label: str, index: int, failed: list[str]) -> None:
        _core.emit(
            "llm.fallback_served", chain_name, model=label, index=index, failed=tuple(failed)
        )

    settings = {
        "llm": "fallback",
        "models": tuple(labels),
        "breakers": breaker is not False,
        "cached_answer": store is not None,
    }

    if is_async:

        @functools.wraps(models[0])
        async def async_chain(*args: Any, **kwargs: Any) -> Any:
            failed: list[str] = []
            last: BaseException | None = None
            for index, (label, call) in enumerate(entries):
                try:
                    result = await call(*args, **kwargs)
                except Exception as exc:
                    if not falls_back(exc):
                        raise
                    failed.append(label)
                    last = exc
                    continue
                if store is not None and result is not None:
                    found = answer_key(args, kwargs)
                    if found is not None:
                        await _maybe_await(store.set(found, (None, result), None))
                served(label, index, failed)
                return result
            if store is not None:
                found = answer_key(args, kwargs)
                entry = None if found is None else await _maybe_await(store.get(found))
                if isinstance(entry, tuple) and len(entry) == 2:
                    served("cached", -1, failed)
                    return entry[1]
            assert last is not None
            raise last

        async_chain.__wrapped__ = models[0]
        setattr(async_chain, _LAYER_ATTR, ("fallback", settings))
        return async_chain

    @functools.wraps(models[0])
    def chain(*args: Any, **kwargs: Any) -> Any:
        failed: list[str] = []
        last: BaseException | None = None
        for index, (label, call) in enumerate(entries):
            try:
                result = call(*args, **kwargs)
            except Exception as exc:
                if not falls_back(exc):
                    raise
                failed.append(label)
                last = exc
                continue
            if store is not None and result is not None:
                found = answer_key(args, kwargs)
                if found is not None:
                    store.set(found, (None, result), None)
            served(label, index, failed)
            return result
        if store is not None:
            found = answer_key(args, kwargs)
            entry = None if found is None else store.get(found)
            if isinstance(entry, tuple) and len(entry) == 2:
                served("cached", -1, failed)
                return entry[1]
        assert last is not None
        raise last

    chain.__wrapped__ = models[0]
    setattr(chain, _LAYER_ATTR, ("fallback", settings))
    return chain


# --------------------------------------------------------------------- stream timeout


class _Limits:
    def __init__(
        self, first_token: float | None, chunk_gap: float | None, total: float | None
    ) -> None:
        for label, value in (
            ("first_token", first_token),
            ("chunk_gap", chunk_gap),
            ("total", total),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{label} must be a positive number of seconds, got {value!r}")
        if first_token is None and chunk_gap is None and total is None:
            raise ValueError("stream_timeout needs first_token=, chunk_gap= or total=")
        self.first_token, self.chunk_gap, self.total = first_token, chunk_gap, total

    def wait(self, started: float, seen_first: bool, now: float) -> tuple[float | None, str, float]:
        """``(seconds left or None, limit name, limit value)`` for the next chunk."""
        candidates: list[tuple[float, str, float]] = []
        if not seen_first and self.first_token is not None:
            candidates.append((started + self.first_token, "first_token", self.first_token))
        if seen_first and self.chunk_gap is not None:
            candidates.append((now + self.chunk_gap, "chunk_gap", self.chunk_gap))
        if self.total is not None:
            candidates.append((started + self.total, "total", self.total))
        if not candidates:
            return None, "", 0.0
        deadline, label, limit = min(candidates)
        return max(deadline - now, 0.0), label, limit

    def settings(self) -> dict[str, Any]:
        return {
            "llm": "stream_timeout",
            "first_token": self.first_token,
            "chunk_gap": self.chunk_gap,
            "total": self.total,
            "seconds": self.total,
        }


def _expired(label: str, limit: float, function: str) -> StreamTimeout:
    _core.emit("timeout.expired", function, seconds=limit)
    what = {
        "first_token": "no first chunk within",
        "chunk_gap": "no new chunk within",
        "total": "stream not finished within",
    }[label]
    return StreamTimeout(f"{function}: {what} {limit:g}s ({label})", seconds=limit, limit=label)


class _SyncStream(Iterator[T]):
    """Pulls a sync iterator in a daemon thread so waits can time out."""

    def __init__(
        self, source: Iterable[T], limits: _Limits, started: float | None, function: str
    ) -> None:
        self._source = source
        self._limits = limits
        self._started = started
        self._function = function
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first = False
        self._done = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __iter__(self) -> _SyncStream[T]:
        return self

    def _put(self, item: tuple[str, Any]) -> bool:
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _pump(self) -> None:
        iterator: Iterator[T] | None = None
        try:
            iterator = iter(self._source)
            for item in iterator:
                if not self._put(("item", item)):
                    break
            else:
                self._put(("end", None))
        except BaseException as exc:
            self._put(("error", exc))
        finally:
            if self._stop.is_set() and iterator is not None:
                close = getattr(iterator, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()

    def __next__(self) -> T:
        if self._done:
            raise StopIteration
        if self._thread is None:
            if self._started is None:
                self._started = time.monotonic()
            context = contextvars.copy_context()
            self._thread = threading.Thread(
                target=context.run, args=(self._pump,), name="stream-timeout", daemon=True
            )
            self._thread.start()
        started = self._started if self._started is not None else time.monotonic()
        left, label, limit = self._limits.wait(started, self._first, time.monotonic())
        try:
            kind, value = self._queue.get(timeout=left)
        except queue.Empty:
            self.close()
            raise _expired(label, limit, self._function) from None
        if kind == "item":
            self._first = True
            return cast(T, value)
        self._done = True
        if kind == "error":
            raise value
        raise StopIteration

    def close(self) -> None:
        """Stop reading; aborts the source if it has a ``close()`` (an HTTP stream)."""
        self._done = True
        self._stop.set()
        if not inspect.isgenerator(self._source):
            close = getattr(self._source, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    def __enter__(self) -> _SyncStream[T]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _AsyncStream(AsyncIterator[T]):
    """Times each ``__anext__`` of an async iterator."""

    def __init__(
        self, source: AsyncIterable[T], limits: _Limits, started: float | None, function: str
    ) -> None:
        self._source = source
        self._limits = limits
        self._started = started
        self._function = function
        self._iterator: AsyncIterator[T] | None = None
        self._first = False
        self._done = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __aiter__(self) -> _AsyncStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._done:
            raise StopAsyncIteration
        if self._iterator is None:
            if self._started is None:
                self._started = time.monotonic()
            self._iterator = self._source.__aiter__()
        started = self._started if self._started is not None else time.monotonic()
        left, label, limit = self._limits.wait(started, self._first, time.monotonic())
        step = asyncio.ensure_future(self._iterator.__anext__())
        done, _ = await asyncio.wait({step}, timeout=left)
        if not done:
            step.cancel()
            await asyncio.wait({step})
            if not step.cancelled():
                step.exception()  # retrieve it: the timeout wins
            await self.aclose()
            raise _expired(label, limit, self._function)
        try:
            item = step.result()
        except StopAsyncIteration:
            self._done = True
            raise
        self._first = True
        return item

    async def aclose(self) -> None:
        """Close the underlying stream (``aclose()`` or ``close()``), once."""
        self._done = True
        target: Any = self._iterator if self._iterator is not None else self._source
        for method in ("aclose", "close"):
            close = getattr(target, method, None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await _maybe_await(close())
                return

    async def __aenter__(self) -> _AsyncStream[T]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _guard(source: Any, limits: _Limits, started: float | None, function: str) -> Any:
    if isinstance(source, AsyncIterable):
        return _AsyncStream(source, limits, started, function)
    if isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        return _SyncStream(source, limits, started, function)
    raise TypeError(f"stream_timeout needs an iterable or async iterable, got {type(source)!r}")


@overload
def stream_timeout(
    source: AsyncIterable[T],
    /,
    *,
    first_token: float | None = ...,
    chunk_gap: float | None = ...,
    total: float | None = ...,
) -> AsyncIterator[T]: ...
@overload
def stream_timeout(
    source: Iterable[T],
    /,
    *,
    first_token: float | None = ...,
    chunk_gap: float | None = ...,
    total: float | None = ...,
) -> Iterator[T]: ...
@overload
def stream_timeout(
    source: None = None,
    /,
    *,
    first_token: float | None = ...,
    chunk_gap: float | None = ...,
    total: float | None = ...,
) -> _Decorator[..., Any]: ...
def stream_timeout(
    source: Any = None,
    /,
    *,
    first_token: float | None = None,
    chunk_gap: float | None = None,
    total: float | None = None,
) -> Any:
    """Time limits for a streamed LLM response: first chunk, gap between chunks, total.

    A plain `timeout` cannot guard a stream: the call that opens it returns at once and
    the waiting happens while you iterate. ``stream_timeout`` limits

    - ``first_token``: seconds until the first chunk (from the call when used as a
      decorator, from the first ``next()`` when wrapping a stream);
    - ``chunk_gap``: seconds to wait for each later chunk;
    - ``total``: seconds for the whole stream, measured from the same start.

    When a limit trips, the stream is closed and `StreamTimeout` (a `TimeoutExpired`,
    so also a ``TimeoutError``) is raised with ``.limit`` naming which one; the event
    ``timeout.expired`` is emitted. Limits run on real time.

    Use it on a stream object (``stream_timeout(client_stream, chunk_gap=10)``) or as a
    decorator on a function returning one: a generator function, an async generator
    function, a sync function returning an iterable, or an ``async def`` returning an
    async iterable (then awaiting it counts against ``first_token`` and ``total``).
    Attributes of the stream (such as an SDK's helper methods) stay reachable.

    Sync streams are read in a daemon thread one chunk ahead, so the wait can time out;
    a read already blocked in that thread only ends when its connection does (sources
    with a ``close()`` method, like HTTP streams, are closed to abort it). Async streams
    have the pending ``__anext__`` cancelled.

    Args:
        source: A stream to wrap, or None for the decorator form.
        first_token: Limit for the first chunk.
        chunk_gap: Limit between chunks.
        total: Limit for the whole stream.

    Returns:
        The guarded stream, or a decorator.

    Raises:
        ValueError: If no limit is given or one is not positive.

    Example:
        ```python
        import time

        from python_wrap.llm import StreamTimeout, stream_timeout

        def chunks():
            yield "Hel"
            yield "lo"
            time.sleep(0.5)  # the provider stalls
            yield "!"

        received = []
        try:
            for chunk in stream_timeout(chunks(), first_token=1, chunk_gap=0.05):
                received.append(chunk)
        except StreamTimeout as exc:
            assert exc.limit == "chunk_gap"
        assert received == ["Hel", "lo"]
        ```
    """
    limits = _Limits(first_token, chunk_gap, total)
    if source is not None:
        return _guard(source, limits, None, type(source).__qualname__)
    settings = limits.settings()

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        label = _core.qualname(func)
        if inspect.isasyncgenfunction(func) or not _core.is_async(func):

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.monotonic()
                return _guard(func(*args, **kwargs), limits, started, label)

            setattr(wrapper, _LAYER_ATTR, ("timeout", settings))
            return cast("Callable[P, R]", wrapper)

        afunc = cast("Callable[..., Awaitable[Any]]", func)

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            left, which, limit = limits.wait(started, False, started)
            opening = asyncio.ensure_future(afunc(*args, **kwargs))
            done, _ = await asyncio.wait({opening}, timeout=left)
            if not done:
                opening.cancel()
                await asyncio.wait({opening})
                if not opening.cancelled():
                    opening.exception()
                raise _expired(which, limit, label)
            return _guard(opening.result(), limits, started, label)

        setattr(async_wrapper, _LAYER_ATTR, ("timeout", settings))
        return cast("Callable[P, R]", async_wrapper)

    setattr(decorate, _LAYER_ATTR, ("timeout", settings))
    return decorate


# --------------------------------------------------------------------------- doctor


def doctor_findings(layers: Sequence[Layer]) -> list[Finding]:
    """PW013-PW015: lint stacks containing LLM layers (called by `python_wrap.doctor`)."""
    from python_wrap.doctor import Finding

    kinds = [layer.settings.get("llm") for layer in layers]
    if not any(kinds):
        return []
    found: list[Finding] = []
    if "retry" in kinds and "budget" not in kinds:
        found.append(
            Finding(
                "PW013",
                "warning",
                "LLM call is retried but has no spend budget: a retry storm or an agent "
                "loop spends without limit. Add a Budget (tokens=/usd=), for example "
                "llm_client(..., spend=Budget(...)) or @budget(...).",
            )
        )
    timeouts = [layer for layer in layers if layer.role == "timeout"]
    if not timeouts:
        found.append(
            Finding(
                "PW014",
                "warning",
                "LLM call has no timeout: a stalled provider holds the caller forever. Add "
                "timeout=timeout(seconds) (llm_client does), or stream_timeout(...) for "
                "streamed responses.",
            )
        )
    for layer in timeouts:
        if (
            layer.settings.get("llm") == "stream_timeout"
            and layer.settings.get("chunk_gap") is None
        ):
            found.append(
                Finding(
                    "PW014",
                    "warning",
                    "stream_timeout without chunk_gap: a stream that stalls after its first "
                    "chunk is only caught by total= (or never). Set chunk_gap=seconds.",
                )
            )
    for layer in layers:
        if (
            layer.role == "retry"
            and layer.settings.get("llm") is None
            and not layer.settings.get("retry_if")
        ):
            found.append(
                Finding(
                    "PW015",
                    "warning",
                    "a generic retry wraps an LLM call without retry_if=: bad requests "
                    "and content-policy refusals are retried (and billed) again. Use "
                    "llm_retry(), or retry(retry_if=llm_transient, "
                    "delay_from=llm_retry_after).",
                )
            )
    return found
