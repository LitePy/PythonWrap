# Migrating to 0.3

0.3 keeps the existing decorator toolkit and adds production integration helpers.
This release remains pure Python, Python 3.10+, and has no runtime dependencies.
Optional Redis, SQLAlchemy, framework and OpenTelemetry integrations use packages
already installed by the application.

## Redaction matching

`log_args` and `audit` now match sensitive **name segments**, including snake_case,
camelCase and separator-delimited names. `author`, `author_id`, `authorised_by`,
`session_id`, `session_date`, and `study_session` are no longer masked just because
they contain `auth` or `session`. Bare `auth`/`session` and secret compound names such as
`auth_token` and `session_key` remain masked. This default fits domain names where
session means an activity rather than an authentication credential.

Review your application's naming rules: an actual secret called `session_id` now needs
an explicit redaction rule. `redact_match="substring"` restores the old matching mode
for a decorator. Custom `redact=` lists replace defaults; prefer `redact_extra=` when
extending defaults.

The standalone `redact(obj)` masks nested mappings/containers, does not modify input,
and masks cycles or containers beyond `max_depth` rather than returning uninspected
secret data. `log_args` and `audit` keep top-level-only matching by default; pass
`deep=True` to mask nested argument mappings/containers with the same selected fragments
and matching mode, or call the standalone helper when preparing payloads elsewhere.
Deep argument redaction does not sanitize opt-in audit results or exception messages. [Redaction guide](../features/observability/logging.md#redaction).

## Retry outcomes and hooks

`delay_from` now receives an exception **or the rejected return value** for result retries.
Exception-only callbacks should check the input type, or return None when they cannot
extract a hint. `retry_after_header` handles both, and `http_status(429, 503)` builds a
result predicate without importing an HTTP client. `json_key=` supports synchronous
response JSON decoders. The sync helper cannot await an async response decoder.

An ordinary `on_retry` hook exception is now logged and swallowed so bookkeeping does not
replace the original failure or rejected value. Cancellation and process-exit exceptions
still propagate. If a hook previously aborted retries intentionally by raising, move that
condition into `retry_if` or the surrounding application code.

`CircuitOpenError` is never retried, even with a broad exception filter. Handle it with a
fallback or wait for the breaker's recovery policy instead of hammering an open circuit.

## External cache counts and clocks

`CacheInfo.currsize` can be **None** when a store intentionally does not count its entries,
including the shipped `RedisStore`. Check for None before arithmetic. It does not scan
Redis on every statistics read. `RedisStore.__len__` returns zero for compatibility with
Python's nonnegative length protocol; `size()` is the unknown-aware API.

Externally stored expiry timestamps use the live wall clock instead of an import-time
offset. Local expiry stays monotonic. `fake_clock()` patches both for deterministic tests.
The old handwritten Redis recipe is replaced by the [shipped adapter](../features/caching/stores-and-credentials.md).

## Forked workers

On platforms with `os.register_at_fork`, registered local locks are recreated in the
child. Memoize cache/in-flight state, once initialization and singleflight coordination
start fresh where supported; in-process limits remain separate for every worker.
A periodic loop inherited from the parent is stopped and warns when inspected/restarted;
call `start()` during child-worker initialization if it should run there. Buffered writers
discard inherited pending records and can start a new child writer lazily.

Do not treat this as a guarantee that arbitrary library clients or application locks are
fork-safe. Create SQLAlchemy engines, network clients and framework state according to
those libraries' worker-lifecycle requirements. Distributed backends are opt-in; no default
cache, limiter or breaker becomes cross-process automatically.

## New APIs

- [Named policies and predicates](../features/resilience/named-policies.md).
- [Shared caches and credentials](../features/caching/stores-and-credentials.md).
- [Leases and idempotency](../features/concurrency/leases-and-idempotency.md).
- [Shared windows, quotas, breakers and bulkheads](../features/concurrency/shared-limits.md).
- [Buffered writers](../features/concurrency/buffered-writes.md).
- [Batch outcomes and checkpoints](../features/correctness/batch-processing.md).
- [Dynamic switches](../features/switches.md).
- [Context, telemetry, slow calls and health](../features/observability/production.md).
- [Framework integrations](../cookbook/framework-integrations.md).

`deadline`, fallback and stale-while-revalidate remain the existing APIs. The new helpers
compose with them rather than replacing them. Leases and stored idempotency results do
not guarantee exactly-once external effects after a crash or lease expiry.
