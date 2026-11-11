<!-- Append this section to your repository's AGENTS.md. -->

## python-wrap (resilience decorators)

This project uses python-wrap for retries, timeouts, circuit breakers, rate limits,
caching and idempotency. Before adding or changing any of these:

1. Read `python -m python_wrap guide` once.
2. Find the API with `python -m python_wrap recipe "<need>"` or
   `python -m python_wrap api search <words>`; check exact parameters with
   `python -m python_wrap api <symbol>`. Do not guess parameter names.
3. Prefer `python_wrap.presets` (`http_client`, `db_call`, `third_party_api`) or
   `policy(...)` over hand-stacked decorators.
4. Layer order, outermost first: observe, validate, fallback, cache, limiter, bulkhead,
   breaker, retry, timeout. `timeout` goes inside `retry`; `circuit_breaker` outside it.
5. Never retry non-idempotent work (payments, e-mails, webhooks, POST without an
   idempotency key): pass `idempotent=False` to presets and deduplicate with
   `@idempotent(key=...)`. Name transient exceptions in `retry(on=...)`; never retry
   4xx other than 408/425/429. Always bound attempts or `max_time`.
6. Breakers, bulkheads and rate limits are per process unless given a shared backend.
7. Lint every stack you touch:
   `python -m python_wrap doctor package.module:attribute [--non-idempotent] [--multiprocess]`
   and fix every error. In tests use `python_wrap.testing.fake_clock()`, not sleeps.
