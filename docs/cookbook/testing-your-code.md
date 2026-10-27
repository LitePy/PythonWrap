# Testing code that uses python-wrap

## The problem

Decorators that wait and remember make tests slow and order-dependent:

- A `retry(delay=30, backoff=2)` makes the failing-path test take minutes.
- A `memoize` leaks results from one test into the next.
- A `rate_limit` or circuit breaker keeps counting across tests, so a test passes alone and fails in the suite.
- A TTL cache or throttle window needs real time to pass.

[`python_wrap.testing`](../testing.md) is built for this. It is standard library only (pytest is optional), and the helpers change python-wrap's behaviour from the outside, so production code stays unchanged.

| Helper | Use it to |
| --- | --- |
| `fast_mode()` | skip every wait (retry backoff, blocking rate limits) but keep the logic |
| `fake_clock()` | control time: make waits instant **and recorded**, and move TTLs, windows and circuit timers forward with `.advance(seconds)` |
| `spy(func)` / `spy_on(obj, name)` | record calls without changing behaviour |
| `disable_caching()`, `disable_rate_limits()`, `disable_retries()` | switch a whole feature off for a test |
| `eventually(assertion)` | wait for background work without a fixed `sleep` |

## The code under test

A small service with a retry, a cache, a rate limit and a circuit breaker, which is a typical day in a real codebase. The tests are a complete, runnable file.

```python
import pytest

from python_wrap import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RateLimitExceeded,
    memoize,
    rate_limit,
    retry,
)
from python_wrap.testing import fake_clock, fast_mode, spy_on


class Gateway:
    """Stands in for a payment/HTTP client. Tests make it fail on demand."""

    def __init__(self) -> None:
        self.failures = 0  # how many calls should still fail

    def charge(self, amount: int) -> str:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("gateway unreachable")
        return f"charged {amount}"


gateway = Gateway()
breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60, on=ConnectionError)


@retry(max_attempts=5, delay=30, backoff=2, on=ConnectionError)  # up to 7.5 minutes of waiting!
def charge_with_retry(amount: int) -> str:
    return gateway.charge(amount)


@breaker
def charge_guarded(amount: int) -> str:
    return gateway.charge(amount)


@memoize(ttl=600)
def exchange_rate(currency: str) -> float:
    return {"EUR": 1.1, "GBP": 1.3}[currency]


@rate_limit(2, 60, key=lambda user: user)
def send_sms(user: str) -> str:
    return f"sms to {user}"


# ---- tests --------------------------------------------------------------------------


def test_retry_succeeds_without_waiting() -> None:
    gateway.failures = 2
    with fast_mode():  # the 30 s and 60 s waits become instant
        assert charge_with_retry(10) == "charged 10"


def test_retry_backoff_schedule_is_what_we_configured() -> None:
    gateway.failures = 3
    with fake_clock() as clock:
        charge_with_retry(10)
    assert clock.sleeps == [30, 60, 120]  # exact, because there is no jitter configured


def test_retry_gives_up_after_max_attempts() -> None:
    gateway.failures = 99
    with fast_mode(), spy_on(gateway, "charge") as charge:
        with pytest.raises(ConnectionError):
            charge_with_retry(10)
    assert charge.call_count == 5  # and the spy proves it, without waiting


def test_cache_entry_expires_by_moving_the_clock() -> None:
    with fake_clock() as clock:  # install it before the first call
        exchange_rate.cache_clear()  # start from a clean slate
        exchange_rate("EUR")
        exchange_rate("EUR")
        assert exchange_rate.cache_info().hits == 1
        clock.advance(601)
        exchange_rate("EUR")
        assert exchange_rate.cache_info().misses == 2  # expired, recomputed


def test_circuit_opens_and_recovers() -> None:
    breaker.reset()  # breaker state outlives a single test
    gateway.failures = 3
    with fake_clock() as clock:
        for _ in range(3):
            with pytest.raises(ConnectionError):
                charge_guarded(1)
        assert breaker.state is CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            charge_guarded(1)  # rejected without calling the gateway

        clock.advance(61)
        assert breaker.state is CircuitState.HALF_OPEN
        assert charge_guarded(1) == "charged 1"  # the trial call succeeds
        assert breaker.state is CircuitState.CLOSED


def test_rate_limit_window_moves_with_the_clock() -> None:
    with fake_clock() as clock:
        send_sms("ana")
        send_sms("ana")
        with pytest.raises(RateLimitExceeded) as info:
            send_sms("ana")
        assert 0 < info.value.retry_after <= 60
        clock.advance(61)
        assert send_sms("ana") == "sms to ana"


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

## How it works

* **`fast_mode()`** makes python-wrap's internal sleep a no-op. Retries still run the configured number of times, so you test the real logic with none of the waiting. Use it when you do not care about the *timing*.
* **`fake_clock()`** goes further: it replaces the clocks python-wrap reads. A sleep advances the fake time instead of waiting and is **recorded** in `clock.sleeps`, so you can assert the exact backoff schedule; `clock.advance(n)` moves time forward for TTLs, rate-limit windows and circuit recovery. Time only moves when you say so, so these tests are deterministic.
* **`spy_on(obj, name)`** wraps an attribute for the duration of the block, records `.calls` (args, kwargs, result, exception, duration) and `.call_count`, and restores the original afterwards. `spy(func)` does the same for a function you own, and works for `async def`.
* Both context managers also work as **decorators** on test functions (`@fast_mode()`), including `async def` tests.

## Pitfalls

!!! warning "State lives on the decorated function"
    Caches, circuit breakers, rate-limit windows and `once` results belong to the function object, which lives as long as the module. Reset them between tests or tests depend on their order: `func.cache_clear()`, `breaker.reset()` / `func.breaker.reset()`, `func.reset()` for `once`, `func.cancel()` for `debounce`. `rate_limit`, `throttle` and `singleflight` have no reset: move the clock with `fake_clock`, use a distinct key per test (as `send_sms("ana")` does), or build the decorated function inside the test.

!!! warning "Install the fake clock before the first call"
    An entry stored under the real clock has a timestamp from a different timeline than the fake one (which starts at 1000.0). If a test warms a cache and *then* enters `fake_clock()`, `advance()` will not expire it. Enter the clock first, or `cache_clear()` inside it, as the TTL test above does.

* `fake_clock` does not control `timeout` deadlines or `debounce` timers; they use real timers. Test `debounce` with `.flush()` and `timeout` with tiny real durations.
* `fast_mode` and `fake_clock` change the process-wide clock, not just the current thread, so worker threads started by the test see them. Do not run tests that use them in parallel threads of the same process (pytest-xdist processes are fine).
* `eventually()` polls with python-wrap's sleep, so inside `fast_mode()` it spins without pausing until its timeout. Keep it outside fast mode when you wait on real threads.

## With pytest fixtures

Enable the bundled plugin once, in the top-level `conftest.py`, to get fixtures instead of context managers, and add an autouse fixture that resets the state you own.

<!-- skip -->
```python
# conftest.py
import pytest

from myapp.billing import breaker, exchange_rate

pytest_plugins = ["python_wrap.testing"]  # adds pw_fake_clock and pw_fast_mode


@pytest.fixture(autouse=True)
def reset_python_wrap_state():
    yield
    exchange_rate.cache_clear()
    breaker.reset()
```

<!-- skip -->
```python
# test_billing.py
from myapp.billing import charge_with_retry


def test_backoff(pw_fake_clock):
    ...
    charge_with_retry(10)
    assert pw_fake_clock.sleeps == [30, 60, 120]


def test_quick(pw_fast_mode):
    ...
```

To switch a feature off for a whole test (not to test it, but because it is in the way), use the `disable_*` helpers:

```python
from python_wrap import memoize, rate_limit, retry
from python_wrap.testing import disable_caching, disable_rate_limits, disable_retries

calls = {"charge": 0, "rate": 0}


@retry(max_attempts=5, delay=0)
def charge() -> None:
    calls["charge"] += 1
    raise ConnectionError("card network down")


@rate_limit(1, 60)
@memoize
def exchange_rate() -> float:
    calls["rate"] += 1
    return 1.08


def test_error_path_makes_one_call() -> None:
    with disable_retries():  # retry makes a single attempt
        try:
            charge()
        except ConnectionError:
            pass
    assert calls["charge"] == 1


def test_each_call_hits_the_backend() -> None:
    with disable_caching(), disable_rate_limits():  # memoize and limiters pass through
        exchange_rate()
        exchange_rate()
    assert calls["rate"] == 2


test_error_path_makes_one_call()
test_each_call_hits_the_backend()
```

`disable_caching()` is the right tool when the cache is incidental. When the test is *about* the cache, use `fake_clock` and `cache_clear()` as above.
