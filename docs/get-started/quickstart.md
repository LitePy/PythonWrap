# Quickstart

In about ten minutes you will take one fragile function and make it resilient, cached, observable and fast to test. Every block on this page runs as written, so you can paste it into a file and try it.

The function is a small weather client. To keep the examples self-contained there is no real network: a fake transport fails in the ways a real one does (connection errors, hangs, outages), and each step only changes the decorators on top.

## 1. Start with a plain, flaky function

Here is the starting point. The fake transport fails twice with `ConnectionError` and then answers:

```python
calls = {"n": 0}

def transport(city: str) -> dict:
    """Stands in for an HTTP call: fails twice, then works."""
    calls["n"] += 1
    if calls["n"] <= 2:
        raise ConnectionError("connection reset")
    return {"city": city, "temp_c": 21}

def get_weather(city: str) -> dict:
    return transport(city)

try:
    get_weather("Cairo")
except ConnectionError as exc:
    print("failed:", exc)
```

One bad packet and the caller gets an exception. Most of those failures are transient, so the right response is to try again.

## 2. Add `retry`

[`retry`](../features/resilience/retry.md) calls the function again when it raises. Name the errors that are worth retrying with `on=`; anything else still fails at once.

```python
from python_wrap import retry

calls = {"n": 0}

def transport(city: str) -> dict:
    calls["n"] += 1
    if calls["n"] <= 2:
        raise ConnectionError("connection reset")
    return {"city": city, "temp_c": 21}

@retry(max_attempts=4, delay=0.01, backoff=2, on=ConnectionError)
def get_weather(city: str) -> dict:
    return transport(city)

assert get_weather("Cairo") == {"city": "Cairo", "temp_c": 21}
assert calls["n"] == 3        # two failures, then success
```

What changed: one line. `delay=0.01, backoff=2` waits 10 ms, then 20 ms, and so on between attempts (use seconds that suit your service in real code). The caller no longer sees the two transient failures.

## 3. Add `timeout`

A retry cannot help with a call that never returns. [`timeout`](../features/resilience/timeout.md) puts a limit on each attempt and raises `TimeoutExpired`, which is also a built-in `TimeoutError`.

```python
import time
from python_wrap import TimeoutExpired, retry, timeout

calls = {"n": 0}

def transport(city: str) -> dict:
    calls["n"] += 1
    if calls["n"] == 1:
        time.sleep(0.5)           # the first call hangs
    return {"city": city, "temp_c": 21}

@retry(max_attempts=3, delay=0.01, on=(ConnectionError, TimeoutExpired))
@timeout(0.05)
def get_weather(city: str) -> dict:
    return transport(city)

assert get_weather("Cairo")["temp_c"] == 21
assert calls["n"] == 2        # the hung attempt was cut off and retried
```

What changed: `@timeout` sits below `@retry`, so each attempt gets its own deadline. Put it above `retry` and one deadline would cover all attempts instead. Order matters; [Stacking](stacking.md) explains the rules, and step 6 hands the ordering to `policy(...)`.

!!! note "Timeouts and threads"
    On Unix in the main thread, `timeout` uses a signal timer and interrupts the call. Elsewhere it runs the call in a worker thread and stops waiting for it, but cannot stop the work. See [Timeout](../features/resilience/timeout.md).

## 4. Add `memoize(ttl=...)`

Weather does not change every second, so asking twice for the same city is wasteful. [`memoize`](../features/caching/memoize.md) remembers results by arguments, and `ttl=` makes them expire.

```python
from python_wrap import memoize

calls = {"n": 0}

@memoize(ttl=60)
def get_weather(city: str) -> dict:
    calls["n"] += 1
    return {"city": city, "temp_c": 21}

get_weather("Cairo")
get_weather("Cairo")
get_weather("Oslo")

assert calls["n"] == 2                       # Cairo was computed once
info = get_weather.cache_info()
assert (info.hits, info.misses) == (1, 2)
```

What changed: repeated calls inside 60 seconds skip the network entirely. Exceptions are never cached, so a failed lookup is tried again next time.

## 5. Protect the service with a `CircuitBreaker`

If the weather service is down, retrying every request makes things worse for everyone. A [`CircuitBreaker`](../features/resilience/circuit-breaker.md) counts consecutive failures and, after a threshold, rejects calls immediately with `CircuitOpenError` until a recovery period has passed.

```python
from python_wrap import CircuitBreaker, CircuitOpenError, CircuitState

calls = {"n": 0}
breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30, on=ConnectionError)

@breaker
def get_weather(city: str) -> dict:
    calls["n"] += 1
    raise ConnectionError("service down")

for _ in range(3):
    try:
        get_weather("Cairo")
    except ConnectionError:
        pass

assert breaker.state is CircuitState.OPEN

try:
    get_weather("Cairo")
except CircuitOpenError as exc:
    print(f"rejected, try again in {exc.retry_after:.0f}s")

assert calls["n"] == 3        # the fourth call never reached the transport
```

What changed: after three failures the service is left alone for 30 seconds, and callers get an instant, explicit error that carries `retry_after`. Because the breaker is an object, you can share one between several functions that call the same service.

## 6. Combine everything with `policy(...)`

You now have four protections and a question: in what order? [`policy`](../features/resilience/policies.md) takes each one by role and applies them in the recommended order, whatever order you list them in.

```python
import time
from python_wrap import (
    CircuitBreaker, TimeoutExpired, memoize, policy, retry, timeout,
)

calls = {"n": 0}

def transport(city: str) -> dict:
    calls["n"] += 1
    if calls["n"] == 1:
        raise ConnectionError("connection reset")
    if calls["n"] == 2:
        time.sleep(0.5)
    return {"city": city, "temp_c": 21}

weather_api = policy(
    cache=memoize(ttl=60),
    breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30,
                           on=(ConnectionError, TimeoutExpired)),
    retry=retry(max_attempts=4, delay=0.01, backoff=2,
                on=(ConnectionError, TimeoutExpired)),
    timeout=timeout(0.05),
    name="weather",
)

@weather_api
def get_weather(city: str) -> dict:
    return transport(city)

assert get_weather("Cairo")["temp_c"] == 21     # survived a reset and a hang
assert calls["n"] == 3
assert get_weather("Cairo")["temp_c"] == 21     # served from the cache
assert calls["n"] == 3
```

What changed: the decorator stack became one named, reusable object. Apply `weather_api` to every function that talks to this service and they share one configuration. The cache is outermost, then the breaker, then retry, then a deadline per attempt, which is the order from [Stacking](stacking.md#a-recommended-order).

## 7. Observe it

Resilience you cannot see is hard to trust. [`count_calls`](../features/observability/metrics.md) keeps call, error and latency statistics on the function, and [`subscribe`](../features/observability/metrics.md) delivers an event for every retry, cache hit and breaker change so you can forward them to your metrics system.

```python
from python_wrap import count_calls, memoize, retry, subscribe

seen = []
unsubscribe = subscribe(lambda event: seen.append(event.name))

calls = {"n": 0}

@count_calls
@memoize(ttl=60)
@retry(max_attempts=3, delay=0.01, on=ConnectionError)
def get_weather(city: str) -> dict:
    calls["n"] += 1
    if calls["n"] == 1:
        raise ConnectionError("connection reset")
    return {"city": city, "temp_c": 21}

get_weather("Cairo")
get_weather("Cairo")
unsubscribe()

stats = get_weather.stats
assert stats.calls == 2 and stats.errors == 0
assert "retry.attempt_failed" in seen
assert "cache.hit" in seen
print(seen)
```

What changed: `@count_calls` outermost counts what the caller experienced (two calls, no errors, even though one attempt failed underneath). The event list shows what happened inside: a failed attempt, a retry that succeeded, a cache miss and then a cache hit. Every event name is listed in [Metrics and events](../features/observability/metrics.md).

## 8. Test it instantly

Retries with real delays make tests slow. [`fast_mode`](../testing/index.md) makes every wait inside python-wrap instant while retries still run, so a test that would take minutes finishes immediately.

```python
import time
from python_wrap import retry
from python_wrap.testing import fast_mode

calls = {"n": 0}

@retry(max_attempts=4, delay=30, backoff=2, on=ConnectionError)
def get_weather(city: str) -> dict:
    calls["n"] += 1
    if calls["n"] < 4:
        raise ConnectionError("connection reset")
    return {"city": city, "temp_c": 21}

def test_recovers_after_three_failures() -> None:
    with fast_mode():
        assert get_weather("Cairo")["temp_c"] == 21
    assert calls["n"] == 4

start = time.monotonic()
test_recovers_after_three_failures()      # would sleep 30 + 60 + 120 seconds
assert time.monotonic() - start < 1
```

What changed: nothing in the production code. The delays are real in production and instant in the test. With pytest you can enable the `pw_fast_mode` and `pw_fake_clock` fixtures instead; see [Testing](../testing/index.md).

## What you built

In production code the whole client is one decorator on one function:

```python
from python_wrap import CircuitBreaker, memoize, policy, retry, timeout

weather_api = policy(
    cache=memoize(ttl=60),
    breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30),
    retry=retry(max_attempts=3, delay=0.5, backoff=2, on=(ConnectionError, TimeoutError)),
    timeout=timeout(5),
    name="weather",
)

@weather_api
def get_weather(city: str) -> dict:
    ...
```

## Next steps

<div class="grid cards" markdown>

-   :material-shield-refresh-outline:{ .lg .middle } __[Resilience](../features/resilience/index.md)__

    ---

    Retry with `Retry-After`, circuit breakers, timeouts, fallbacks and hedged requests in depth.

-   :material-database-clock-outline:{ .lg .middle } __[Caching](../features/caching/index.md)__

    ---

    `memoize`, `once`, `cached_property`, batching and `singleflight`.

-   :material-speedometer:{ .lg .middle } __[Concurrency](../features/concurrency/index.md)__

    ---

    Rate limits, bulkheads, throttle and debounce, async helpers.

-   :material-chart-line:{ .lg .middle } __[Observability](../features/observability/index.md)__

    ---

    Logging, audit trails, timing and metrics for Prometheus and OpenTelemetry.

-   :material-chef-hat:{ .lg .middle } __[Cookbook](../cookbook/index.md)__

    ---

    A complete [resilient HTTP client](../cookbook/resilient-http-client.md), FastAPI, Django and more.

-   :material-alert-octagon-outline:{ .lg .middle } __[Errors](errors.md)__

    ---

    Every exception python-wrap raises, and what to catch.

</div>
