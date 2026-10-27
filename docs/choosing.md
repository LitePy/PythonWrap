# Which decorator do I need?

Find your problem, use the decorator in the second column.

## Things fail

| Problem | Use | Guide |
| --- | --- | --- |
| A network call fails now and then | `@retry(on=ConnectionError, delay=..., backoff=2)` | [Resilience](guides/resilience.md#retry) |
| I want a default value instead of an exception | `@fallback(value)` or `@fallback(handler=fn)` | [Resilience](guides/resilience.md#fallback) |
| I just want an error to go away (returns `None`) | `@suppress(KeyError)` | [Resilience](guides/resilience.md#suppress) |
| A dependency is down and I keep hammering it | `@circuit_breaker(...)` or a shared `CircuitBreaker` | [Resilience](guides/resilience.md#circuit-breaker) |
| A call can hang forever | `@timeout(seconds)` | [Resilience](guides/resilience.md#timeout) |

## Things are slow or repeated

| Problem | Use | Guide |
| --- | --- | --- |
| Same arguments, same answer, compute it once | `@memoize` | [Caching](guides/caching.md#memoize) |
| Cached values should go stale after N seconds | `@memoize(ttl=...)` | [Caching](guides/caching.md#expiry-and-size) |
| Expensive one-time setup | `@once` | [Caching](guides/caching.md#once) |
| 100 concurrent requests for the same thing | `@singleflight` | [Concurrency](guides/concurrency.md#singleflight) |
| Run a blocking function in the background | `@run_in_thread` | [Concurrency](guides/concurrency.md#run_in_thread) |

## Things happen too often

| Problem | Use | Guide |
| --- | --- | --- |
| Reject calls over N per second | `@rate_limit(n, period)` | [Concurrency](guides/concurrency.md#rate_limit) |
| Slow callers down instead of rejecting them | `@rate_limit(n, period, block=True)` | [Concurrency](guides/concurrency.md#rate_limit) |
| One limit per user or API key | `@rate_limit(..., key=...)` | [Concurrency](guides/concurrency.md#rate_limit) |
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
