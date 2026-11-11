# Using python-wrap correctly (guide for coding agents)

python-wrap is a typed, zero-dependency resilience toolkit: retry, circuit breakers,
timeouts, rate limits, caching, bulkheads, idempotency and policies. Every decorator
works on sync functions, `async def` functions and methods.

## Find the right API before writing code

    python -m python_wrap recipe "retry http calls"   # plain-language need -> API
    python -m python_wrap api search circuit breaker  # search names and docstrings
    python -m python_wrap api retry                   # signature, params, raises, example
    python -m python_wrap api --json                  # the whole index, machine-readable
    python -m python_wrap mcp                         # the same, as an MCP server

Do not guess parameter names: check them with `api SYMBOL`.

## Prefer a policy or a preset over hand-stacked decorators

`policy(...)` puts layers in the canonical order wherever you pass them. The presets in
`python_wrap.presets` (`http_client`, `db_call`, `third_party_api`) come with timeouts,
bounded attempts, jittered backoff, a `RetryBudget` and a breaker.

    from python_wrap.presets import http_client
    users_api = http_client("users", timeout_seconds=3)

    @users_api
    def get_user(user_id: int) -> dict: ...

## Layer order (outermost first)

    observe -> validate -> fallback -> cache -> limiter -> bulkhead -> breaker -> retry -> timeout -> guard

When stacking by hand, the decorator at the top is the outermost. So:

    @circuit_breaker(...)   # counts calls whose retries were all used up
    @retry(...)             # retries each failed attempt
    @timeout(2)             # bounds each single attempt
    def call(): ...

- `timeout` goes **inside** `retry` (per attempt). A `timeout` above `retry` bounds the
  whole call; then narrow `retry(on=...)` so it does not swallow `TimeoutExpired`.
- `circuit_breaker` goes **outside** `retry`; below it, one unlucky call trips the breaker.
- `memoize` goes outside `retry` and limits: a cache hit should skip them, and failures
  are not cached.
- `rate_limit` outside `retry` spends one token per logical call.

## Idempotency rules

- Only retry operations that are safe to repeat: reads, PUT/DELETE, writes keyed by an
  idempotency key.
- Payments, e-mails, SMS, webhooks, non-idempotent POSTs, queue publishes without
  deduplication: **no retry layer**. Pass `idempotent=False` to presets (the retry layer is
  left out) and use `@idempotent(key=...)` to deduplicate repeated requests.
- `@idempotent` across processes needs a shared result store **and** a shared lease
  backend (`RedisStore(fail_open=False)` supplies a matching `RedisLock`). It cannot give
  exactly-once effects across a crash; use an outbox or database idempotency keys for that.

## What never to retry

- Programming errors (`TypeError`, `KeyError`, `AttributeError`, `ValueError` from your
  own validation): always name the transient exceptions with `on=` (or `retry_if=`);
  `on=Exception` retries bugs (doctor PW003).
- HTTP 4xx other than 408, 425 and 429 (bad request, auth, not found, conflict on a
  non-idempotent write). Honour `Retry-After` with `delay_from=retry_after_header`.
- Authentication and permission failures, validation errors, `CircuitOpenError`,
  `RateLimitExceeded` from your own limiter, `QuotaExhausted`.
- Cancellation (`asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`): python-wrap
  never retries these, do not try to.

Always bound retries with `max_attempts` or `max_time` (PW001) and share a `RetryBudget`
between callers of one dependency (PW008).

## Common mistakes

- Unbounded retries, or retries with no per-attempt timeout (PW001, PW004).
- Timeout not shorter than the retry `max_time` (PW005).
- Two retry layers (for example an HTTP client with built-in retries plus `@retry`):
  attempts multiply (PW007).
- A sync `timeout` cannot preempt code off the main thread or on Windows (PW009): poll
  `checkpoint()` in long loops, or use `async def`.
- `memoize` on a method with no bound pins every instance (PW011): use
  `per_instance=True`, `maxsize=` or `ttl=`.
- Breakers, bulkheads and rate limits are **per process** by default. Under gunicorn,
  uvicorn workers, Celery prefork or process pools, N workers allow N times the limit
  (PW012): use `RedisBreakerState`/`SQLiteBreakerState`, `RedisWindow` and lease backends.
- Calling `time.sleep` in tests: use `python_wrap.testing.fake_clock()` instead.
- Logging raw arguments: use `log_args`/`audit`, which redact sensitive names.
- Configuring logging inside library code: python-wrap never does, neither should you.

## Check your work with the doctor

    python -m python_wrap doctor package.module:policy_or_function
    python -m python_wrap doctor --non-idempotent package.payments:charge
    python -m python_wrap doctor --multiprocess --strict package.clients:users_api

In tests:

    from python_wrap.doctor import explain
    assert explain(users_api, idempotent=True).ok

Exit status 1 means an error (or any finding with `--strict`). Fix every error; review
every warning. Finding codes PW001-PW012 are listed in the docs
(features/resilience/presets-and-doctor).
