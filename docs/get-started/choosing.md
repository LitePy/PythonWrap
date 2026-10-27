# Choosing a decorator

Find your problem, use the decorator in the second column.

## Things fail

| Problem | Use | Guide |
| --- | --- | --- |
| A network call fails now and then | `@retry(on=ConnectionError, delay=..., backoff=2)` | [Resilience](../features/resilience/retry.md) |
| I want a default value instead of an exception | `@fallback(value)` or `@fallback(handler=fn)` | [Resilience](../features/resilience/fallback.md) |
| I just want an error to go away (returns `None`) | `@suppress(KeyError)` | [Resilience](../features/resilience/fallback.md) |
| A dependency is down and I keep hammering it | `@circuit_breaker(...)` or a shared `CircuitBreaker` | [Resilience](../features/resilience/circuit-breaker.md) |
| A call can hang forever | `@timeout(seconds)` or `with timeout(seconds):` | [Resilience](../features/resilience/timeout.md) |
| The server says `Retry-After` | `@retry(delay_from=retry_after_header)` | [Resilience](../features/resilience/retry.md) |
| Retry only part of a function | `for attempt in retrying(...)` | [Resilience](../features/resilience/retry.md) |
| A whole request needs one time budget | `with deadline(seconds):` | [Resilience](../features/resilience/timeout.md) |
| A read is occasionally very slow | `@hedge(delay)` (idempotent calls only) | [Batching and hedging](../features/resilience/hedge.md) |
| Apply retry, breaker and timeout in the right order | `policy(retry=..., breaker=..., timeout=...)` | [Policies](../features/resilience/policies.md) |

## Things are slow or repeated

| Problem | Use | Guide |
| --- | --- | --- |
| Same arguments, same answer, compute it once | `@memoize` | [Caching](../features/caching/memoize.md) |
| Cached values should go stale after N seconds | `@memoize(ttl=...)` | [Caching](../features/caching/memoize.md) |
| Expensive one-time setup | `@once` | [Caching](../features/caching/once.md) |
| 100 concurrent requests for the same thing | `@singleflight` | [Concurrency](../features/caching/singleflight.md) |
| An expensive attribute on an object | `@cached_property(ttl=...)` | [Caching](../features/caching/cached-property.md) |
| Run a blocking function in the background | `@run_in_thread` | [Concurrency](../features/concurrency/run-in-thread.md) |

## Too many things at once

| Problem | Use | Guide |
| --- | --- | --- |
| `gather` thousands of coroutines without flooding a server | `await gather_limit(*coros, limit=20)` | [Async toolkit](../features/concurrency/async-helpers.md) |
| Apply an async function to a list, a few at a time | `await map_async(fn, items, limit=20)` | [Async toolkit](../features/concurrency/async-helpers.md) |
| Take whichever of several calls answers first | `await race(a(), b())` | [Async toolkit](../features/concurrency/async-helpers.md) |
| Call async code from a script | `run_sync(coro)` | [Async toolkit](../features/concurrency/async-helpers.md) |
| Protect a dependency from overload; fail fast when full | `Bulkhead(n, max_waiting=...)` or `@limit_concurrency(n)` | [Async toolkit](../features/concurrency/bulkheads.md) |
| Many single lookups should become one bulk query | `@batch_calls(max_size=..., max_wait=...)` | [Batching and hedging](../features/caching/batching.md) |

## Things happen too often

| Problem | Use | Guide |
| --- | --- | --- |
| Reject calls over N per second | `@rate_limit(n, period)` | [Concurrency](../features/concurrency/rate-limits.md) |
| Slow callers down instead of rejecting them | `@rate_limit(n, period, block=True)` | [Concurrency](../features/concurrency/rate-limits.md) |
| One limit per user or API key | `@rate_limit(..., key=...)` | [Concurrency](../features/concurrency/rate-limits.md) |
| Several functions share one API quota | `RateLimiter(n, period)` | [Concurrency](../features/concurrency/rate-limits.md) |
| Run at most once per interval, skip the rest | `@throttle(interval)` | [Concurrency](../features/concurrency/throttle-debounce.md) |
| Wait for a burst of calls to settle, then run once | `@debounce(wait)` | [Concurrency](../features/concurrency/throttle-debounce.md) |

## I need to see what is happening

| Problem | Use | Guide |
| --- | --- | --- |
| How long does this take? | `@timing` | [Observability](../features/observability/timing.md) |
| Send durations to my metrics system | `@timing(callback=...)` | [Observability](../features/observability/timing.md) |
| Compare how fast a function is over many runs | `benchmark(func, ...)` | [Observability](../features/observability/timing.md) |
| How much memory does it allocate? | `@measure_memory` | [Observability](../features/observability/timing.md) |
| Where does the time go? | `@profile` | [Observability](../features/observability/timing.md) |
| Log the arguments (without leaking passwords) | `@log_args` | [Observability](../features/observability/logging.md) |
| Log what a function returns | `@log_return` | [Observability](../features/observability/logging.md) |
| Keep an audit trail of who called what | `@audit` | [Observability](../features/observability/audit.md) |
| Log the call stack that led here | `@trace` | [Observability](../features/observability/logging.md) |
| Count calls, errors and p95 latency per function | `@count_calls` then `func.stats` | [Metrics and events](../features/observability/metrics.md) |
| Feed retries, cache hits and breaker trips to Prometheus or OpenTelemetry | `subscribe(listener)` | [Metrics and events](../features/observability/metrics.md) |
| Put a request id on every log line | `with log_context(request_id=...):` | [Observability](../features/observability/logging.md) |

## Bad input

| Problem | Use | Guide |
| --- | --- | --- |
| Check arguments with my own rules | `@validate_args(x=lambda x: x > 0)` | [Validation](../features/correctness/validation.md) |
| `isinstance` checks on a few arguments | `@check_type(x=int)` | [Validation](../features/correctness/validation.md) |
| Enforce my type annotations at runtime | `@enforce_types` | [Validation](../features/correctness/validation.md) |

## Half-finished work

| Problem | Use | Guide |
| --- | --- | --- |
| Undo changes to `self` if a method raises | `@revert_on_failure` | [State safety](../features/correctness/state.md) |
| Register undo steps and run them if anything fails | `@transactional` / `Transaction` | [State safety](../features/correctness/state.md) |

## Maintaining and testing code

| Problem | Use | Guide |
| --- | --- | --- |
| Tests that wait on retries and backoff | `with fast_mode():` from `python_wrap.testing` | [Testing](../testing/index.md) |
| Switch retries, rate limits or caches off in a test | `disable_retries()`, `disable_rate_limits()`, `disable_caching()` | [Testing](../testing/index.md) |
| Control time in a test | `FakeClock` / `fake_clock()` | [Testing](../testing/index.md) |
| Set the same logger, retry defaults or redaction everywhere | `configure(...)` | [Configuration](../features/configuration.md) |
| Protect every method of a class | `@wrap_methods(...)` | [Developer tools](../features/devtools.md) |
| Warn callers that an API is going away | `@deprecate(since=..., alternative=...)` | [Developer tools](../features/devtools.md) |
| Return fixed data in tests or local runs | `@mock_data(...)` | [Developer tools](../features/devtools.md) |
| Reuse a stack of decorators | `compose(a, b, c)` | [Developer tools](../features/devtools.md) |

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

    The roles decide the order, so listing them differently changes nothing. See [Policies](../features/resilience/policies.md).
