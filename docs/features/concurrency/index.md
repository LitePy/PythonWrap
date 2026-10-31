# Concurrency and limits

Control how often a function runs, how many calls run at once, and how you run many awaitables or blocking calls without hand-writing the plumbing. Everything here is built for both threads and `asyncio`.

<div class="grid cards" markdown>

-   :material-speedometer:{ .lg .middle } **[Rate limits](rate-limits.md)**

    ---

    `rate_limit` and `RateLimiter`: at most N calls per time window, reject or wait.

-   :material-timer-sand:{ .lg .middle } **[Throttle and debounce](throttle-debounce.md)**

    ---

    Collapse bursts of calls: run at most once per interval, or once after the calls stop.

-   :material-gate:{ .lg .middle } **[Bulkheads](bulkheads.md)**

    ---

    `Bulkhead` and `limit_concurrency`: cap concurrent calls and bound the queue.

-   :material-sync:{ .lg .middle } **[Async helpers](async-helpers.md)**

    ---

    `gather_limit`, `map_async`, `amap_iter`, `race` and `run_sync`.

-   :material-run-fast:{ .lg .middle } **[run_in_thread](run-in-thread.md)**

    ---

    Start a blocking call in a thread and get a `Future` back.

-   :material-update:{ .lg .middle } **[Periodic tasks](periodic.md)**

    ---

    `periodic`: run a function every N seconds in the background, with clean shutdown.

</div>

## Which one?

| You want to... | Use |
| --- | --- |
| Allow at most N calls per time window (an API quota) | [Rate limits](rate-limits.md) |
| Share one quota between several functions | [`RateLimiter`](rate-limits.md#one-budget-for-several-functions-ratelimiter) |
| Skip calls that arrive too soon after the last one | [`throttle`](throttle-debounce.md#throttle) |
| Run once, after a burst of calls has stopped | [`debounce`](throttle-debounce.md#debounce) |
| Cap how many calls run at the same time | [Bulkheads](bulkheads.md) |
| Run many awaitables or items with a bound | [Async helpers](async-helpers.md) |
| Take the first successful result of several awaitables | [`race`](async-helpers.md#race) |
| Call blocking code without waiting for it | [`run_in_thread`](run-in-thread.md) |
| Run something every N seconds in the background | [`periodic`](periodic.md) |
| Make identical concurrent calls share one execution | [`singleflight`](../caching/singleflight.md) |

A rate limit counts calls over **time**; a bulkhead counts calls **in flight**. Use both when a dependency has a quota and a connection limit.

## See also

- [Timeout](../resilience/timeout.md) and [Hedge](../resilience/hedge.md) for deadlines and parallel retries.
- [Testing](../../testing/index.md) for `fast_mode()`, `fake_clock()` and `disable_rate_limits()`.

## Worker coordination

- [Leases, single-instance work and idempotency](leases-and-idempotency.md).
- [Shared Redis windows, quotas, breaker state and bulkheads](shared-limits.md).
- [Bounded background batch writers](buffered-writes.md).
