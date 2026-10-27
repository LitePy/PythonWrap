# Which decorator do I need?

Find your problem, use the decorator in the second column.

## Things fail

| Problem | Use | Guide |
| --- | --- | --- |
| A network call fails now and then | `@retry(on=ConnectionError, delay=..., backoff=2)` | [Resilience](guides/resilience.md#retry) |
| I want a default value instead of an exception | `@fallback(value)` or `@fallback(handler=fn)` | [Resilience](guides/resilience.md#fallback) |
| I just want an error to go away (returns `None`) | `@suppress(KeyError)` | [Resilience](guides/resilience.md#suppress) |
| A dependency is down and I keep hammering it | `@circuit_breaker(...)` or a shared `CircuitBreaker` | [Resilience](guides/resilience.md#circuit-breaker) |
| A call can hang forever | `@timeout(seconds)` or `with timeout(seconds):` | [Resilience](guides/resilience.md#timeout) |
| The server says `Retry-After` | `@retry(delay_from=retry_after_header)` | [Resilience](guides/resilience.md#honour-retry-after) |
| Retry only part of a function | `for attempt in retrying(...)` | [Resilience](guides/resilience.md#retrying-a-block-of-code) |
| A whole request needs one time budget | `with deadline(seconds):` | [Resilience](guides/resilience.md#deadlines-one-budget-for-many-steps) |
| A read is occasionally very slow | `@hedge(delay)` (idempotent calls only) | [Batching and hedging](guides/batching.md#hedge) |
| Apply retry, breaker and timeout in the right order | `policy(retry=..., breaker=..., timeout=...)` | [Policies](guides/policies.md) |

## Things are slow or repeated

| Problem | Use | Guide |
| --- | --- | --- |
| Same arguments, same answer, compute it once | `@memoize` | [Caching](guides/caching.md#memoize) |
| Cached values should go stale after N seconds | `@memoize(ttl=...)` | [Caching](guides/caching.md#expiry-and-size) |
| Expensive one-time setup | `@once` | [Caching](guides/caching.md#once) |
| 100 concurrent requests for the same thing | `@singleflight` | [Concurrency](guides/concurrency.md#singleflight) |
| An expensive attribute on an object | `@cached_property(ttl=...)` | [Caching](guides/caching.md#cached_property) |
| Run a blocking function in the background | `@run_in_thread` | [Concurrency](guides/concurrency.md#run_in_thread) |

## Too many things at once

| Problem | Use | Guide |
| --- | --- | --- |
| `gather` thousands of coroutines without flooding a server | `await gather_limit(*coros, limit=20)` | [Async toolkit](guides/async-toolkit.md#gather_limit) |
| Apply an async function to a list, a few at a time | `await map_async(fn, items, limit=20)` | [Async toolkit](guides/async-toolkit.md#map_async-and-amap_iter) |
| Take whichever of several calls answers first | `await race(a(), b())` | [Async toolkit](guides/async-toolkit.md#race) |
| Call async code from a script | `run_sync(coro)` | [Async toolkit](guides/async-toolkit.md#run_sync) |
| Protect a dependency from overload; fail fast when full | `Bulkhead(n, max_waiting=...)` or `@limit_concurrency(n)` | [Async toolkit](guides/async-toolkit.md#bulkhead) |
| Many single lookups should become one bulk query | `@batch_calls(max_size=..., max_wait=...)` | [Batching and hedging](guides/batching.md#batch_calls) |

## Things happen too often

| Problem | Use | Guide |
| --- | --- | --- |
| Reject calls over N per second | `@rate_limit(n, period)` | [Concurrency](guides/concurrency.md#rate_limit) |
| Slow callers down instead of rejecting them | `@rate_limit(n, period, block=True)` | [Concurrency](guides/concurrency.md#rate_limit) |
| One limit per user or API key | `@rate_limit(..., key=...)` | [Concurrency](guides/concurrency.md#one-limit-per-user) |
| Several functions share one API quota | `RateLimiter(n, period)` | [Concurrency](guides/concurrency.md#one-budget-for-several-functions-ratelimiter) |
| Run at most once per interval, skip the rest | `@throttle(interval)` | [Concurrency](guides/concurrency.md#throttle) |
| Wait for a burst of calls to settle, then run once | `@debounce(wait)` | [Concurrency](guides/concurrency.md#debounce) |

## I need to see what is happening

| Problem | Use | Guide |
| --- | --- | --- |
| How long does this take? | `@timing` | [Observability](guides/observability.md#timing) |
| Send durations to my metrics system | `@timing(callback=...)` | [Observability](guides/observability.md#timing) |
| Compare how fast a function is over many runs | `benchmark(func, ...)` | [Observability](guides/observability.md#benchmark) |
| How much memory does it allocate? | `@measure_memory` | [Observability](guides/observability.md#measure_memory) |
| Where does the time go? | `@profile` | [Observability](guides/observability.md#profile) |
| Log the arguments (without leaking passwords) | `@log_args` | [Observability](guides/observability.md#log_args-and-log_return) |
| Log what a function returns | `@log_return` | [Observability](guides/observability.md#log_args-and-log_return) |
| Keep an audit trail of who called what | `@audit` | [Observability](guides/observability.md#audit) |
| Log the call stack that led here | `@trace` | [Observability](guides/observability.md#trace) |
| Count calls, errors and p95 latency per function | `@count_calls` then `func.stats` | [Metrics and events](guides/metrics.md#count_calls) |
| Feed retries, cache hits and breaker trips to Prometheus or OpenTelemetry | `subscribe(listener)` | [Metrics and events](guides/metrics.md#events) |
| Put a request id on every log line | `with log_context(request_id=...):` | [Observability](guides/observability.md#log_context) |

## Bad input

| Problem | Use | Guide |
| --- | --- | --- |
| Check arguments with my own rules | `@validate_args(x=lambda x: x > 0)` | [Validation](guides/validation.md#validate_args) |
| `isinstance` checks on a few arguments | `@check_type(x=int)` | [Validation](guides/validation.md#check_type) |
| Enforce my type annotations at runtime | `@enforce_types` | [Validation](guides/validation.md#enforce_types) |

## Half-finished work

| Problem | Use | Guide |
| --- | --- | --- |
| Undo changes to `self` if a method raises | `@revert_on_failure` | [State safety](guides/state.md#revert_on_failure) |
| Register undo steps and run them if anything fails | `@transactional` / `Transaction` | [State safety](guides/state.md#transactions) |

## Maintaining and testing code

| Problem | Use | Guide |
| --- | --- | --- |
| Tests that wait on retries and backoff | `with fast_mode():` from `python_wrap.testing` | [Testing](testing.md) |
| Switch retries, rate limits or caches off in a test | `disable_retries()`, `disable_rate_limits()`, `disable_caching()` | [Testing](testing.md) |
| Control time in a test | `FakeClock` / `fake_clock()` | [Testing](testing.md) |
| Set the same logger, retry defaults or redaction everywhere | `configure(...)` | [Configuration](guides/configuration.md) |
| Protect every method of a class | `@wrap_methods(...)` | [Developer tools](guides/devtools.md#wrap_methods) |
| Warn callers that an API is going away | `@deprecate(since=..., alternative=...)` | [Developer tools](guides/devtools.md#deprecate) |
| Return fixed data in tests or local runs | `@mock_data(...)` | [Developer tools](guides/devtools.md#mock_data) |
| Reuse a stack of decorators | `compose(a, b, c)` | [Developer tools](guides/devtools.md#compose) |

## Common recipes

??? example "Robust HTTP call"

    ```python
    from python_wrap import circuit_breaker, retry, timeout

    @circuit_breaker(failure_threshold=5, recovery_timeout=30)
    @retry(max_attempts=3, delay=0.2, backoff=2, jitter=0.1, on=(ConnectionError, TimeoutError))
    @timeout(5)
    def call_api() -> dict:
        ...
    ```

    Each attempt gets its own 5 second deadline, and the breaker counts a call as failed only once its retries are used up. See [Stacking decorators](stacking.md).

??? example "Cached, de-duplicated lookup"

    ```python
    from python_wrap import memoize, singleflight

    @memoize(ttl=30)
    @singleflight
    def load_price(sku: str) -> float:
        ...
    ```

??? example "Public API with input checks"

    ```python
    from python_wrap import enforce_types, log_args, validate_args

    @log_args
    @enforce_types
    @validate_args(amount=(lambda a: a > 0, "amount must be positive"))
    def charge(account: str, amount: float) -> None:
        ...
    ```

??? example "The same thing as one named policy"

    ```python
    from python_wrap import CircuitBreaker, policy, retry, retry_after_header, timeout

    api = policy(
        breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30),
        retry=retry(max_attempts=3, delay=0.2, backoff=2, on=ConnectionError,
                    delay_from=retry_after_header),
        timeout=timeout(5),
        name="api",
    )

    @api
    def call_api() -> dict:
        ...
    ```

    The roles decide the order, so listing them differently changes nothing. See [Policies](guides/policies.md).
