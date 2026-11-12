---
name: python-wrap
description: Use when adding or changing retries, timeouts, circuit breakers, rate limits, caching, bulkheads or idempotency in Python code that uses (or should use) the python-wrap package. Looks up the right API, applies the layer-order and idempotency rules, and verifies the result with the python-wrap doctor.
---

# python-wrap

python-wrap is a typed, zero-dependency resilience toolkit. Follow these steps whenever
you touch resilience code.

## 1. Learn the rules (once per session)

Run `python -m python_wrap guide` and follow it.

## 2. Choose the API

- `python -m python_wrap recipe "<what you need>"` maps a need ("retry http calls",
  "stop duplicate emails", "limit llm tokens") to the API, with a runnable snippet.
- `python -m python_wrap api search <words>` searches every public name.
- `python -m python_wrap api <symbol>` (or `--json`) gives the exact signature,
  parameters, raised exceptions and an example. Never guess parameter names.

Prefer a preset (`python_wrap.presets.http_client`, `db_call`, `third_party_api`) or
`policy(...)` over stacking decorators by hand.

## 3. Apply the rules

- Order, outermost first: observe, validate, fallback, cache, limiter, bulkhead, breaker,
  retry, timeout.
- Retry only idempotent operations; name transient exceptions in `on=`; bound attempts
  or `max_time`; never retry 4xx other than 408/425/429.
- Payments, e-mails, webhooks: `idempotent=False` on presets, `@idempotent(key=...)`.
- Under several worker processes, give breakers and limits a shared backend.

## 4. Verify

Run `python -m python_wrap doctor package.module:attribute` for every policy or function
you changed (add `--non-idempotent` for side effects, `--multiprocess` under worker
pools). Fix every error, review every warning, and test with
`python_wrap.testing.fake_clock()` instead of sleeps.
