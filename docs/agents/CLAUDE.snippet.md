<!-- Append this section to your project's CLAUDE.md. -->

## python-wrap

Resilience code (retry, timeout, circuit breaker, rate limit, cache, idempotency) uses
python-wrap. Rules:

- Run `python -m python_wrap guide` before the first change to resilience code.
- Look APIs up, do not guess: `python -m python_wrap recipe "<need>"`,
  `python -m python_wrap api search <words>`, `python -m python_wrap api <symbol>`.
  If the python-wrap MCP server is configured, use its `recipe`, `get_symbol` and
  `doctor` tools instead.
- Prefer `python_wrap.presets` or `policy(...)`; hand-stacked order, outermost first:
  observe, validate, fallback, cache, limiter, bulkhead, breaker, retry, timeout.
- Never retry side effects that are not idempotent (charges, e-mails, webhooks):
  `idempotent=False` on presets, `@idempotent(key=...)` to deduplicate.
- `retry` always gets `on=` with transient exceptions and `max_attempts` or `max_time`.
- After editing a stack, run
  `python -m python_wrap doctor package.module:attribute` (add `--non-idempotent` for
  side effects, `--multiprocess` under worker pools) and fix every error.
- Tests use `python_wrap.testing.fake_clock()` instead of real sleeps.
