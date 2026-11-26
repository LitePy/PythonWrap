"""Opinionated, documented starting points for the most common resilience stacks.

Choosing numbers is the hard part of resilience. Each preset returns an ordinary
`Policy` with a per-attempt timeout, a bounded retry guarded by a `RetryBudget`, and a
circuit breaker, using values that are safe for typical traffic:

- `http_client`: 10 s timeout, 3 attempts, waits 0.2 s then 0.4 s (plus up to 0.1 s
  jitter), breaker opens after 5 failures for 30 s.
- `db_call`: 5 s timeout, 3 attempts on transient database errors only, waits 0.05 s then
  0.1 s, no breaker by default.
- `third_party_api`: 15 s timeout, 4 attempts, waits 0.5, 1, 2 s (plus up to 0.5 s),
  breaker opens after 5 failures for 60 s; optional rate limit and bulkhead.
- `llm_client`: 60 s timeout, 4 attempts on transient LLM errors only (429, 529,
  overloaded, 5xx, timeouts, connection errors), waits 1, 2, 4 s (plus up to 0.5 s) or the
  provider's ``Retry-After``, breaker counting only those errors opens after 5 for 60 s;
  optional spend `Budget`.

Every retry layer gets ``max_time`` larger than ``timeout * attempts`` so a slow
attempt never consumes the whole budget, and a `RetryBudget` (20 % of traffic plus 5
retries/s) so retries cannot multiply load during an outage. Pass ``idempotent=False``
for operations that must not run twice: the retry layer is then left out entirely.

The presets are part of the stable core: the defaults may only change in a minor
release with a CHANGELOG entry, never in a patch release.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python_wrap.bulkhead import Bulkhead
from python_wrap.circuit import CircuitBreaker
from python_wrap.concurrency import RateLimiter, timeout
from python_wrap.errors import CircuitOpenError, TimeoutExpired
from python_wrap.llm import Budget, TokenRateLimiter, _BreakerLayer, _llm_breaker, llm_retry
from python_wrap.policy import Policy, policy
from python_wrap.predicates import db_transient
from python_wrap.retry import RetryBudget, retry
from python_wrap.shared import SharedBackends, resolve_shared

__all__ = ["TRANSPORT_ERRORS", "db_call", "http_client", "llm_client", "third_party_api"]

#: Exceptions every preset treats as transient: lost/refused connections and timeouts
#: (`TimeoutExpired` is a `TimeoutError`). HTTP libraries' own transport errors subclass
#: neither, so pass ``on=`` (or use `python_wrap.http`) for them.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)


def _budget(
    budget: RetryBudget | None, name: str = "", shared: SharedBackends | None = None
) -> RetryBudget:
    if budget is not None:
        return budget
    backend = None if shared is None else shared.budget_backend(name)
    return RetryBudget(ratio=0.2, min_per_second=5, backend=backend)


def _breaker(shared: SharedBackends | None, **options: Any) -> CircuitBreaker:
    return CircuitBreaker(backend=None if shared is None else shared.breaker(), **options)


def _retry(
    *,
    idempotent: bool,
    attempts: int,
    seconds: float,
    delay: float,
    max_delay: float,
    jitter: float,
    on: tuple[type[BaseException], ...] | type[BaseException],
    retry_if: Callable[[Any], bool] | None,
    budget: RetryBudget | None,
    name: str = "",
    shared: SharedBackends | None = None,
) -> Any:
    if not idempotent or attempts <= 1:
        return None
    return retry(
        max_attempts=attempts,
        delay=delay,
        backoff=2,
        max_delay=max_delay,
        jitter=jitter,
        max_time=seconds * attempts + delay * 2**attempts + 1,
        on=on,
        retry_if=retry_if,
        budget=_budget(budget, name, shared),
    )


def http_client(
    name: str,
    *,
    timeout_seconds: float = 10.0,
    attempts: int = 3,
    idempotent: bool = True,
    breaker: bool = True,
    on: tuple[type[BaseException], ...] = TRANSPORT_ERRORS,
    budget: RetryBudget | None = None,
    shared: Any = None,
) -> Policy:
    """A policy for calls to an internal HTTP service you own.

    Args:
        name: Policy and breaker name (used in events, metrics and errors).
        timeout_seconds: Per-attempt time limit.
        attempts: Total attempts, including the first.
        idempotent: False leaves the retry layer out (POSTs without an idempotency key).
        breaker: Include a `CircuitBreaker` (5 failures, 30 s recovery).
        on: Exception types that are retried and counted by the breaker.
        budget: Share one `RetryBudget` between several policies; a new one otherwise.
        shared: A Redis client or `SharedBackends`: the breaker state and the retry budget
            then live in Redis, keyed by ``name``, so every worker process shares them
            (default: ``configure(shared=...)``, else per-process state).

    Returns:
        A `Policy`, usable as a decorator.

    Example:
        ```python
        from python_wrap.presets import http_client

        users = http_client("users-service")

        @users
        def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        assert get_user(7) == {"id": 7}
        assert users.explain().ok
        ```
    """
    sb = resolve_shared(shared)
    return policy(
        name=name,
        breaker=(
            _breaker(sb, failure_threshold=5, recovery_timeout=30.0, on=on, name=name)
            if breaker
            else None
        ),
        retry=_retry(
            idempotent=idempotent,
            attempts=attempts,
            seconds=timeout_seconds,
            delay=0.2,
            max_delay=2.0,
            jitter=0.1,
            on=on,
            retry_if=None,
            budget=budget,
            name=name,
            shared=sb,
        ),
        timeout=timeout(timeout_seconds),
    )


def db_call(
    name: str,
    *,
    timeout_seconds: float = 5.0,
    attempts: int = 3,
    idempotent: bool = True,
    breaker: bool = False,
    retry_if: Callable[[Any], bool] = db_transient,
    budget: RetryBudget | None = None,
    shared: Any = None,
) -> Policy:
    """A policy for a database unit of work (a whole transaction, not one statement).

    Retries only errors `db_transient` classifies as transient (deadlocks, serialization
    failures, dropped connections, Azure SQL reconfiguration), so constraint violations
    and syntax errors fail at once. The breaker is off by default: a database is usually
    a hard dependency, and failing fast rarely helps. Turn it on for optional/secondary
    databases; it then counts only connection-level failures (`TRANSPORT_ERRORS`).

    Args:
        name: Policy name.
        timeout_seconds: Per-attempt time limit (keep it above your statement timeout).
        attempts: Total attempts.
        idempotent: False leaves the retry layer out.
        breaker: Include a `CircuitBreaker` (10 failures, 15 s recovery).
        retry_if: Which exceptions are transient; default `db_transient`.
        budget: A shared `RetryBudget`, or None for a new one.
        shared: A Redis client or `SharedBackends` for breaker state and the retry budget
            (see `http_client`).

    Returns:
        A `Policy`.
    """
    sb = resolve_shared(shared)
    return policy(
        name=name,
        breaker=(
            _breaker(
                sb, failure_threshold=10, recovery_timeout=15.0, on=TRANSPORT_ERRORS, name=name
            )
            if breaker
            else None
        ),
        retry=_retry(
            idempotent=idempotent,
            attempts=attempts,
            seconds=timeout_seconds,
            delay=0.05,
            max_delay=1.0,
            jitter=0.05,
            on=Exception,
            retry_if=lambda exc: (
                not isinstance(exc, (CircuitOpenError, TimeoutExpired)) and retry_if(exc)
            ),
            budget=budget,
            name=name,
            shared=sb,
        ),
        timeout=timeout(timeout_seconds),
    )


def third_party_api(
    name: str,
    *,
    timeout_seconds: float = 15.0,
    attempts: int = 4,
    idempotent: bool = True,
    on: tuple[type[BaseException], ...] = TRANSPORT_ERRORS,
    calls: int | None = None,
    period: float = 1.0,
    max_concurrent: int | None = None,
    budget: RetryBudget | None = None,
    shared: Any = None,
) -> Policy:
    """A policy for an external vendor API (payment gateway, ERP, SMS/e-mail provider).

    Slower backoff than `http_client` and a longer breaker recovery, because vendors
    recover on their own schedule. Optional client-side rate limit (``calls`` per
    ``period``, rejecting with `RateLimitExceeded`) and bulkhead (``max_concurrent``)
    keep you inside the vendor's quota and stop one slow vendor from tying up every
    worker.

    Args:
        name: Policy, breaker and bulkhead name.
        timeout_seconds: Per-attempt time limit.
        attempts: Total attempts.
        idempotent: False leaves the retry layer out (charges, sends).
        on: Exception types that are retried and counted by the breaker.
        calls: Rate limit: calls allowed per ``period`` (None: no limiter).
        period: Rate-limit period in seconds.
        max_concurrent: Bulkhead size (None: no bulkhead).
        budget: A shared `RetryBudget`, or None for a new one.
        shared: A Redis client or `SharedBackends`: breaker state, the rate window, the
            bulkhead lease and the retry budget are then shared by every worker process,
            so ``calls`` is a fleet-wide limit (see `http_client`).

    Returns:
        A `Policy`.
    """
    sb = resolve_shared(shared)
    window = None if sb is None or calls is None else sb.rate_window(name, calls, period)
    lease = None if sb is None else sb.lease_backend()
    return policy(
        name=name,
        limiter=(
            None
            if calls is None
            else RateLimiter(calls, period, name=name, backend=window)
            if window is not None
            else RateLimiter(calls, period, name=name)
        ),
        bulkhead=(
            None if max_concurrent is None else Bulkhead(max_concurrent, name=name, backend=lease)
        ),
        breaker=_breaker(sb, failure_threshold=5, recovery_timeout=60.0, on=on, name=name),
        retry=_retry(
            idempotent=idempotent,
            attempts=attempts,
            seconds=timeout_seconds,
            delay=0.5,
            max_delay=8.0,
            jitter=0.5,
            on=on,
            retry_if=None,
            budget=budget,
            name=name,
            shared=sb,
        ),
        timeout=timeout(timeout_seconds),
    )


def llm_client(
    name: str,
    *,
    timeout_seconds: float = 60.0,
    attempts: int = 4,
    idempotent: bool = True,
    breaker: bool = True,
    max_delay: float = 60.0,
    budget: RetryBudget | None = None,
    spend: Budget | None = None,
    tokens: TokenRateLimiter | None = None,
    shared: Any = None,
) -> Policy:
    """A policy for calls to an LLM provider (chat/completions/messages endpoints).

    Retries only what `python_wrap.llm.llm_transient` accepts: 408/409/429/529, every
    5xx, overloaded bodies, timeouts and connection errors. Bad requests, auth errors and
    content-policy refusals fail at once. Waits double from 1 s (plus up to 0.5 s jitter)
    unless the provider sends ``Retry-After`` / ``retry-after-ms``, which is honoured up
    to ``max_delay``. The breaker counts only those transient errors, so a burst of
    refused prompts never opens it. The 60 s per-attempt timeout suits non-streamed
    answers of a few thousand tokens; raise it for long outputs and use
    `python_wrap.llm.stream_timeout` for streams.

    Args:
        name: Policy and breaker name.
        timeout_seconds: Per-attempt time limit.
        attempts: Total attempts, including the first.
        idempotent: False leaves the retry layer out.
        breaker: Include a breaker (5 transient failures, 60 s recovery).
        max_delay: Longest wait between attempts, also the cap on ``Retry-After``.
        budget: A shared `RetryBudget`, or None for a new one (20 %, 5/s).
        spend: A token/dollar `python_wrap.llm.Budget`, placed in the ``limiter`` role
            (checked once per logical call; usage recorded from the final response).
        tokens: A `python_wrap.llm.TokenRateLimiter`, also in the ``limiter`` role.
        shared: A Redis client or `SharedBackends` for the breaker state and the retry
            budget (see `http_client`).

    Returns:
        A `Policy`.

    Example:
        ```python
        from python_wrap.llm import Budget
        from python_wrap.presets import llm_client

        chat = llm_client("chat", spend=Budget(tokens=100_000))

        @chat
        def complete(prompt: str) -> dict[str, object]:
            return {"usage": {"input_tokens": 10, "output_tokens": 5}}

        complete("hi")
        assert chat.explain().findings == ()
        ```
    """
    sb = resolve_shared(shared)
    limiters = [layer for layer in (spend, tokens) if layer is not None]
    return policy(
        name=name,
        limiter=limiters or None,
        breaker=(
            _BreakerLayer(
                _llm_breaker(
                    failure_threshold=5,
                    recovery_timeout=60.0,
                    name=name,
                    backend=None if sb is None else sb.breaker(),
                )
            )
            if breaker
            else None
        ),
        retry=(
            llm_retry(
                max_attempts=attempts,
                delay=1.0,
                max_delay=max_delay,
                jitter=0.5,
                max_time=timeout_seconds * attempts + max_delay * (attempts - 1) + 1,
                budget=_budget(budget, name, sb),
            )
            if idempotent and attempts > 1
            else None
        ),
        timeout=timeout(timeout_seconds),
    )
