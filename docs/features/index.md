# Features

Every feature works on plain functions, `async def` functions and methods, keeps the
wrapped function's signature for your type checker, and has no dependencies. Pick the
area that matches your problem.

<div class="grid cards" markdown>

-   :material-shield-refresh-outline:{ .lg .middle } __[Resilience](resilience/index.md)__

    ---

    Survive flaky dependencies: retry with backoff and `Retry-After`, circuit breaker,
    timeouts and deadlines, fallbacks, hedged requests, and policies that combine them.

-   :material-cached:{ .lg .middle } __[Caching and batching](caching/index.md)__

    ---

    Do less work: `memoize` with TTL and LRU, `once`, `cached_property`, collapse duplicate
    calls with `singleflight`, and turn many lookups into one with `batch_calls`.

-   :material-speedometer:{ .lg .middle } __[Concurrency and limits](concurrency/index.md)__

    ---

    Stay within limits: rate limits, throttle and debounce, bulkheads, bounded async
    helpers (`gather_limit`, `map_async`, `race`) and `run_in_thread`.

-   :material-chart-timeline-variant:{ .lg .middle } __[Observability](observability/index.md)__

    ---

    See what your code does: argument and result logging with redaction, audit records,
    timing and profiling, call metrics, and one event hook for Prometheus or OpenTelemetry.

-   :material-check-decagram-outline:{ .lg .middle } __[Correctness](correctness/index.md)__

    ---

    Catch bad input and keep state consistent: argument validation, runtime type checks,
    rollback on failure and transactions.

-   :material-tools:{ .lg .middle } __[Developer tools](devtools.md)__

    ---

    `deprecate`, `mock_data`, `compose`, `wrap_methods` and `no_debug` for everyday
    library and application work.

-   :material-tune-variant:{ .lg .middle } __[Configuration](configuration.md)__

    ---

    Set project-wide defaults once: the logger, retry options and redaction lists.

-   :material-test-tube:{ .lg .middle } __[Testing](../testing/index.md)__

    ---

    Make code that retries, sleeps and rate-limits instant and deterministic in tests.

</div>

## Production tools

0.3 adds [named policies and predicates](resilience/named-policies.md),
[shared stores and credentials](caching/stores-and-credentials.md),
[leases and idempotency](concurrency/leases-and-idempotency.md),
[shared limits](concurrency/shared-limits.md), [buffered writers](concurrency/buffered-writes.md),
[batch processing](correctness/batch-processing.md), [switches](switches.md),
and [context, telemetry and health](observability/production.md).
Optional [framework integrations](../cookbook/framework-integrations.md) bind request/task context.
