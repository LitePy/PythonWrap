<div align="center">

<a href="https://pythonwrap.readthedocs.io">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/LitePy/PythonWrap/main/docs/assets/logo-dark.svg">
    <img alt="python-wrap" src="https://raw.githubusercontent.com/LitePy/PythonWrap/main/docs/assets/logo-light.svg" height="64">
  </picture>
</a>

**The resilience and concurrency toolkit you add to every Python project.**<br>
Retry (with `Retry-After`), circuit breaker, timeout, cache, rate limit, bulkhead, batching, async helpers, metrics and test helpers. One import, sync and async, zero dependencies.

[![CI](https://github.com/LitePy/PythonWrap/actions/workflows/ci.yml/badge.svg)](https://github.com/LitePy/PythonWrap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/python-wrap?logo=pypi&logoColor=white)](https://pypi.org/project/python-wrap/)
[![Python](https://img.shields.io/pypi/pyversions/python-wrap?logo=python&logoColor=white)](https://pypi.org/project/python-wrap/)
[![License: MIT](https://img.shields.io/pypi/l/python-wrap)](https://github.com/LitePy/PythonWrap/blob/main/LICENSE)
[![Docs](https://readthedocs.org/projects/pythonwrap/badge/?version=latest)](https://pythonwrap.readthedocs.io)
[![Typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-2a6db2)](https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict)

[**Documentation**](https://pythonwrap.readthedocs.io) ·
[Cookbook](https://pythonwrap.readthedocs.io/en/latest/cookbook/) ·
[Performance](https://pythonwrap.readthedocs.io/en/latest/project/performance/) ·
[Migration guide](https://pythonwrap.readthedocs.io/en/latest/project/migration/) ·
[Changelog](https://github.com/LitePy/PythonWrap/blob/main/CHANGELOG.md)

</div>


## Why python-wrap

- **One import for the things every project hand-writes.** Retry, circuit breaker, timeout, cache, rate limit, bulkhead, batching, bounded `gather`, metrics and test helpers, designed to stack and to be tested together.
- **Resilience that behaves.** `retry` honours `Retry-After`, a time budget and cancellation; a shared `deadline()` caps a whole request; `policy(...)` stacks the protections in the right order for you.
- **Zero dependencies.** Pure Python, nothing to audit or pin.
- **Typed.** Decorators preserve the wrapped signature (`ParamSpec`), and stacking keeps the exact type (`@retry` over `@memoize` still has `.cache_info()`). Ships `py.typed`, checked with `mypy --strict`.
- **Sync and async, one API.** Works on `def`, `async def` and methods. Async wrappers stay coroutine functions and never block the event loop.
- **Fast.** Cache hits and validation skip signature binding on the hot path ([numbers](https://pythonwrap.readthedocs.io/en/latest/project/performance/)).
- **Observable and testable.** One event stream for retries, cache, circuit, limiters and timeouts; `python_wrap.testing` makes tests instant and deterministic.
- **Never configures your logging**, is thread-safe, and is tested on Python 3.10 to 3.14 on Linux, macOS and Windows.

## Install

```bash
pip install python-wrap
# or
uv add python-wrap
```

Python 3.10 or newer.

## 30-second tour

```python
import asyncio
import time

from python_wrap import (
    CircuitBreaker, gather_limit, memoize, policy, rate_limit, retry, retry_after_header, timeout,
)
from python_wrap.testing import fast_mode

# One reusable stack for a flaky HTTP dependency. Roles decide the order, not you.
http = policy(
    cache=memoize(ttl=60, maxsize=256),                       # hits skip everything below
    breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30, name="api"),
    retry=retry(max_attempts=4, delay=0.5, backoff=2, jitter=0.1,
                on=ConnectionError, delay_from=retry_after_header, max_time=20),
    timeout=timeout(5),                                       # per attempt
    name="http",
)


@http
def fetch_user(user_id: int) -> dict[str, int]:
    return {"id": user_id}                 # your real HTTP call goes here


print(fetch_user(1), fetch_user(1))        # the second one is a cache hit
print(fetch_user.cache_info())             # the cache API survives the stack


@rate_limit(100, 1.0, block=True)          # at most 100 calls per second, waits when full
async def fetch_async(user_id: int) -> dict[str, int]:
    await asyncio.sleep(0.01)
    return {"id": user_id}


async def main() -> None:
    # 50 requests, never more than 20 in flight; results come back in order.
    users = await gather_limit(*(fetch_async(i) for i in range(50)), limit=20)
    print(len(users), "users")


asyncio.run(main())


# In tests, every wait is instant but retries still run the configured number of times.
calls = []


@retry(max_attempts=4, delay=30, backoff=2, on=ConnectionError)
def flaky() -> str:
    calls.append(1)
    if len(calls) < 4:
        raise ConnectionError
    return "ok"


start = time.monotonic()
with fast_mode():
    assert flaky() == "ok"                 # would take 30 + 60 + 120 seconds
print(f"{time.monotonic() - start:.2f}s")
```

## What is in the box

Everything below is importable from `python_wrap` unless a module is shown. [Choose a decorator](https://pythonwrap.readthedocs.io/en/latest/get-started/choosing/) is the quick way in.

| Area | Name | What it does |
|---|---|---|
| **Resilience** | `retry` | Backoff, jitter, `max_delay`, `max_time`, `retry_if`, `retry_if_result`, `on_retry` (async hooks too); `delay_from=retry_after_header` honours `Retry-After`; never retries cancellation |
| | `RetryBudget` | One retry allowance shared across functions, so a failing dependency does not trigger a retry storm |
| | `retrying`, `Retrying` | Retry an inline block (`for attempt in retrying(...)`, sync and `async for`) |
| | `retry_stats`, `RetryStats` | Attempts, successes and failures per retried function |
| | `fallback`, `suppress` | Return a default (or handler result), or swallow chosen exceptions |
| | `circuit_breaker`, `CircuitBreaker` | Fail fast while a dependency is down; `success_threshold`, `exclude`, `on_state_change`, `.stats`; one breaker can guard many functions |
| | `timeout`, `Timeout` | Decorator or `with timeout(5):` / `async with`; raises `TimeoutExpired`; `cleanup_grace=` bounds async cleanup |
| | `deadline`, `Deadline` | A shared time budget; `retry` and `timeout` clamp to it |
| | `hedge` | Fire a backup call when the first is slow and take the first success (idempotent calls only) |
| **Async toolkit** | `gather_limit`, `map_async`, `amap_iter` | Bounded concurrency, lazy input, ordered results; everything is cancelled and awaited on error |
| | `race`, `RaceFailed` | First success wins, the rest are cancelled |
| | `run_sync` | Call async code from sync code (refuses inside a running loop) |
| **Caching** | `memoize` | LRU + TTL, `typed`, `key=`, per-item `ttl=`, `cache_exceptions=`, `stale_ttl=` (serve stale while refreshing), `per_instance=True`, `cache_info/clear/delete/contains/get`; async in-flight dedupe |
| | `CacheStore` | Protocol to keep `memoize` entries in your own store (dict, Redis, ...) |
| | `cached_property`, `invalidate` | `functools.cached_property` plus `ttl`, locking and `async def` getters |
| | `once`, `cache` | Run once and reuse (thread-safe, `.reset()`); `cache` is the v0.1 name for `memoize(ttl=...)` |
| **Concurrency & limits** | `rate_limit`, `RateLimiter` | Sliding window, `block=True`, `key=`, several limits, one budget shared across functions |
| | `Bulkhead`, `limit_concurrency` | Cap concurrent calls, bound the queue, raise `BulkheadFull` |
| | `throttle`, `debounce` | At most once per interval; run after calls stop (`.cancel()`, `.flush()`) |
| | `singleflight`, `run_in_thread` | Share one execution between identical concurrent calls; run in a thread, get a `Future` |
| | `periodic` | Run a function every N seconds in the background, without drift, with clean `start()`/`stop()` |
| **Batching** | `batch_calls` | DataLoader pattern: many single-item calls become one bulk call; sync and async, deduped |
| **Observability & metrics** | `count_calls` | `.stats`: calls, errors, in-flight, mean, p50, p95, p99 |
| | `subscribe`, `listening` | One event hook for retry, cache, circuit, limiter, bulkhead and timeout events (`python_wrap.events` also has `filter_events`, `EVENT_NAMES`) |
| | `log_args`, `log_return`, `trace`, `audit` | Structured, redacted logging; `AuditRecord` to a sink; `trace(otel=True)` opens an OpenTelemetry span |
| | `log_context`, `current_log_context` | Contextvar fields in every log line and audit record |
| | `timing`, `profile`, `measure_memory`, `benchmark` | Duration with `threshold=`/`callback=`, `cProfile`, `tracemalloc`, `benchmark_async` |
| **Validation** | `enforce_types`, `validate_args`, `check_type` | Check from annotations, with predicates, or with explicit types |
| **State** | `transactional`, `Transaction`, `current_transaction` | Commit and rollback callbacks, usable as a (async) context manager |
| | `revert_on_failure` | Restore an object's attributes if the call raises |
| **Configuration** | `configure`, `configured`, `get_config`, `reset_config` | Project-wide defaults for logger, retry options and redaction |
| | `policy`, `Policy` | Named, immutable decorator bundles applied in the recommended order |
| **Testing** (`python_wrap.testing`) | `fast_mode`, `disable_retries`, `disable_rate_limits`, `disable_caching`, `overrides` | Skip waits, cap retries, switch limiters and caches off |
| | `FakeClock`, `fake_clock`, `spy`, `spy_on`, `eventually` | Control time, record calls, wait for background work; pytest fixtures `pw_fake_clock`, `pw_fast_mode` |
| **Dev tools** | `deprecate`, `mock_data`, `compose`, `wrap_methods` | Deprecation warnings at the caller, fixed test data, decorator bundles, decorate a whole class |

Exceptions live in `python_wrap.errors` and at the top level: `PythonWrapError`, `TimeoutExpired` (a `TimeoutError`), `RateLimitExceeded`, `CircuitOpenError`, `BulkheadFull`, `BatchError`, `ValidationError` (a `ValueError`) and `ArgumentTypeError` (a `TypeError`).

## Highlights

### Retry, `Retry-After` and fail-soft

```python
from python_wrap import RetryState, retry


def note(state: RetryState) -> None:
    print(f"attempt {state.attempt} failed: {state.exception!r}")


calls = 0


@retry(
    max_attempts=5,
    delay=0.01,       # wait before the 2nd attempt
    backoff=2.0,      # then 2x longer each time
    max_delay=0.1,    # but never more than this
    jitter=0.01,      # plus up to this many random seconds
    max_time=5,       # and never spend longer than this in total
    on=(ConnectionError, TimeoutError),
    retry_if=lambda exc: "temporary" in str(exc),
    on_retry=note,
)
def flaky() -> str:
    global calls
    calls += 1
    if calls < 3:
        raise ConnectionError("temporary glitch")
    return "ok"


print(flaky())  # ok, after two retries
```

Fail soft with `fallback(value)` or `suppress(OSError)`. The last exception is re-raised unchanged once attempts run out. `max_attempts=None` retries forever. Cancellation (`asyncio.CancelledError`, `KeyboardInterrupt`) is never retried. Add `delay_from=retry_after_header` to wait exactly as long as a 429/503 `Retry-After` header says, and retry a block of code with `for attempt in retrying(...)`.

### Circuit breaker, timeout and deadline

```python
import time

from python_wrap import CircuitBreaker, CircuitOpenError, CircuitState, TimeoutExpired, deadline, timeout

payments = CircuitBreaker(failure_threshold=2, recovery_timeout=30, name="payments")


@payments
def charge(amount: int) -> str:
    raise ConnectionError("gateway down")


for _ in range(2):
    try:
        charge(10)
    except ConnectionError:
        pass

try:
    charge(10)
except CircuitOpenError:
    print("failing fast, circuit is", payments.state.value)   # no call was made
assert payments.state is CircuitState.OPEN


@timeout(1)
def step(seconds: float) -> None:
    time.sleep(seconds)


try:
    with deadline(0.05):          # one budget for everything inside; timeouts clamp to it
        step(0.01)
        step(1)
except TimeoutExpired:
    print("out of time")
```

After `recovery_timeout` the breaker goes half-open and lets a trial call through. Share one `CircuitBreaker` across functions for one dependency, or use `@circuit_breaker(...)` for a private one (`func.breaker`). `timeout` interrupts sync code with `SIGALRM` on the Unix main thread, cancels `async def` with `asyncio.wait_for`, and elsewhere falls back to a thread that cannot stop the work (see [Limitations](https://pythonwrap.readthedocs.io/en/latest/project/limitations/)).

### Caching

```python
from python_wrap import memoize


@memoize(maxsize=128, ttl=30)
def square(n: int, *, scale: int = 1) -> int:
    return n * n * scale


square(3)
square(n=3)                 # same entry: arguments are bound to the signature
square(3, scale=1)          # still the same entry
print(square.cache_info())  # 2 hits, 1 miss
square.cache_delete(3)      # drop one entry
square.cache_clear()        # drop everything

```

`cached_property(ttl=)` does the same for attributes. Keys are built from the bound signature, lists, dicts and sets are converted automatically, and exceptions are not cached unless you ask (`cache_exceptions=True, exception_ttl=`). Concurrent awaits of the same missing key in an `async def` share one in-flight task. On methods the cache keeps instances alive, so give it `maxsize` or `ttl`.

### Async toolkit, bulkheads and batching

```python
import asyncio

from python_wrap import Bulkhead, BulkheadFull, batch_calls, map_async, race

queries: list[list[int]] = []


@batch_calls(max_size=100, max_wait=0.005)        # the DataLoader pattern
async def get_user(ids: list[int]) -> dict[int, str]:
    queries.append(ids)                           # one round trip for the whole batch
    return {i: f"user{i}" for i in ids}


db = Bulkhead(2, max_waiting=1, name="db")        # 2 running, 1 queued, the rest rejected


@db
async def query(n: int) -> int:
    await asyncio.sleep(0.01)
    return n


async def main() -> None:
    users = await asyncio.gather(get_user(1), get_user(2), get_user(1))
    assert users == ["user1", "user2", "user1"] and queries == [[1, 2]]

    doubled = await map_async(lambda n: query(n * 2), range(3), limit=2)
    assert doubled == [0, 2, 4]

    results = await asyncio.gather(*(query(n) for n in range(4)), return_exceptions=True)
    assert isinstance(results[3], BulkheadFull)

    async def slow() -> str:
        await asyncio.sleep(1)
        return "slow"

    async def fast() -> str:
        return "fast"

    assert await race(slow(), fast()) == "fast"   # the loser is cancelled


asyncio.run(main())
```

Every helper cancels and awaits the tasks it started, so nothing leaks on error or cancellation. Also see `hedge` for backup requests on slow reads and `run_sync` for calling async code from scripts.

### Rate limits, throttle and debounce

```python
from python_wrap import RateLimitExceeded, RateLimiter, rate_limit


@rate_limit(2, 0.2, key=lambda user, *_: user)   # 2 calls / 0.2 s, per user
def send(user: str, text: str) -> str:
    return f"{user}: {text}"


send("ann", "a")
send("ann", "b")
try:
    send("ann", "c")
except RateLimitExceeded as exc:
    print(f"slow down, retry in {exc.retry_after:.2f}s")
send("bob", "hello")        # different key, different budget

github = RateLimiter(5, 1.0, name="github")      # one budget shared by several functions


@github
def get_repo(name: str) -> str:
    return name

```

`rate_limit(block=True)` waits instead of raising (with `await` for `async def`), and `limits=[(5, 1), (600, 900)]` enforces several windows at once. `throttle` and `debounce` cover "at most once per interval" and "after the burst settles". The sliding window never lets a `2 * calls` burst through at a window edge.

### Metrics and events

```python
from python_wrap import count_calls, listening, retry


@count_calls
@retry(max_attempts=3, delay=0)
def work(attempts: list[int]) -> str:
    attempts.append(1)
    if len(attempts) < 3:
        raise ConnectionError
    return "ok"


names: list[str] = []
with listening(lambda event: names.append(event.name)):
    work([])

print(names)                          # retry.attempt_failed, ..., retry.succeeded
print(work.stats.calls, work.stats.p95)
```

Subscribe once and forward events to Prometheus, OpenTelemetry or statsd (recipes in the [guide](https://pythonwrap.readthedocs.io/en/latest/features/observability/metrics/)). Listeners cannot break the decorated function.

### Validation and logging with redaction

```python
import logging

from python_wrap import ArgumentTypeError, enforce_types, log_args

logging.basicConfig(level=logging.DEBUG, format="%(message)s")   # your app configures logging


@enforce_types
def total(prices: list[float], currency: str = "USD") -> str:
    return f"{sum(prices):.2f} {currency}"


print(total([1.5, 2.5]))
try:
    total([1.5, "2.5"])      # type: ignore[list-item]
except ArgumentTypeError as exc:
    print(exc)               # parameter 'prices' expected list[float], got list containing str at index 1


@log_args(logger="myapp.auth")
def login(username: str, password: str, api_key: str = "") -> bool:
    return True


login("ann", "hunter2", api_key="sk-123")
# Calling login(username='ann', password='***', api_key='***')
```

Parameters whose names contain `password`, `token`, `secret`, `api_key` and similar (`DEFAULT_REDACT`) are masked in `log_args` and `audit`; extend with `redact_extra=` or project-wide through `configure(redact_extra=...)`. For custom rules use `validate_args(x=lambda x: x > 0)`.

### Transactions and defaults

`@transactional` gives a function a `tx: Transaction` (declare it last or keyword-only) to register `on_commit` and `on_rollback` callbacks; `current_transaction()` reaches it from anywhere. `configure(logger=..., redact_extra=...)` sets project-wide defaults read at decoration time, and explicit arguments always win.

## Stacking decorators and policies

Decorators are applied bottom-up and run top-down, so order matters:

| Put this... | ...outside of this | Because |
|---|---|---|
| `memoize` | `retry`, `rate_limit`, `timeout` | Cache hits skip everything; only misses pay |
| `retry` | `timeout`, `circuit_breaker`, `rate_limit` | Each attempt gets its own deadline |
| `circuit_breaker` | `retry` or `timeout` | The breaker counts calls whose retries were used up |
| `fallback` | `retry`, `circuit_breaker` | Fall back only after retries are exhausted or the circuit is open |
| `log_args`, `timing`, `audit` | everything | They observe what callers actually experience |
| `enforce_types`, `validate_args` | the rest | Reject bad input before doing any work |

You do not have to memorise this. `policy(observe=, validate=, fallback=, cache=, limiter=, bulkhead=, breaker=, retry=, timeout=, guard=)` takes each decorator by role and applies them in the recommended order, whatever order you wrote them in. Policies are immutable (`.with_(timeout=timeout(30))`), work on methods and `async def`, and `wrap_methods(policy)` protects a whole class. `compose(a, b, c)(f)` is `a(b(c(f)))` when you just want a named stack. Details: [Stacking](https://pythonwrap.readthedocs.io/en/latest/get-started/stacking/) and [Policies](https://pythonwrap.readthedocs.io/en/latest/features/resilience/policies/).

## How it compares

python-wrap is a broad toolkit; the others are deeper in one area. Use them when that one area is all you need and you want the specialist.

| Library | Use it instead when... | python-wrap's angle |
|---|---|---|
| [tenacity](https://github.com/jd/tenacity) | You need very rich retry policies: composable stop/wait strategies, per-call overrides | `retry` covers the common cases (backoff, jitter, `Retry-After`, time budget, hooks, async) with a flat signature, and shares a deadline, breaker and events with the rest |
| [stamina](https://github.com/hynek/stamina) | You want opinionated, production-ready retries with Prometheus and structlog integration built in | Similar defaults-first retry, plus the breaker, timeout, caches and limits it composes with; metrics via a generic event hook |
| [backoff](https://github.com/litl/backoff) | You already use it and are happy. The repository was archived in 2025, so it no longer receives fixes | A maintained, typed retry with the same core ideas |
| [cachetools](https://github.com/tkem/cachetools) | You need other eviction policies (LFU, FIFO, RR), explicit cache objects or custom locks | `memoize` gives LRU + TTL, `cache_info()`, async in-flight dedupe, signature-bound keys and `cached_property(ttl=)` |
| [async-lru](https://github.com/aio-libs/async-lru) | You only cache coroutines and want a tiny dependency | Same async dedupe, also for sync code, with TTL, per-item TTL and exception caching |
| [aiolimiter](https://github.com/mjpieters/aiolimiter) | You only need an asyncio leaky-bucket limiter | Sliding window for sync and async, per-key buckets, shared `RateLimiter`, `block=True`, plus `Bulkhead` for concurrency |
| [pybreaker](https://github.com/danielfm/pybreaker) | You need listeners or circuit state shared across processes (Redis storage) | A typed `CircuitBreaker` with async, `exclude=`, `success_threshold` and `.stats`, no dependency |
| [functools](https://docs.python.org/3/library/functools.html) | A plain `lru_cache` or `cache` on a sync function with hashable arguments is enough. Prefer it for that | Adds TTL, async, unhashable arguments and `cache_delete` |

None of them cover bounded `gather`, bulkheads, batching, hedging, policies or test helpers; python-wrap trades per-area depth for one consistent, typed, dependency-free API.

## Learn more

- [Cookbook](https://pythonwrap.readthedocs.io/en/latest/cookbook/): resilient HTTP client, async crawler, FastAPI, Django, CLI tools, DataLoader batching, testing, observability.
- [Performance](https://pythonwrap.readthedocs.io/en/latest/project/performance/): per-call overhead and how to benchmark it yourself.
- [Testing](https://pythonwrap.readthedocs.io/en/latest/testing/): `fast_mode`, `FakeClock`, `spy` and pytest fixtures.
- [Migration guide](https://pythonwrap.readthedocs.io/en/latest/project/migration/) and [CHANGELOG](https://github.com/LitePy/PythonWrap/blob/main/CHANGELOG.md).
- Runnable [examples](https://github.com/LitePy/PythonWrap/tree/main/examples).

## Upgrading from 0.1

0.2.0 fixes decorators that did not do what their names promised (`transactional`, `revert_on_failure`, async `timing`) and makes a few breaking changes: `run_in_thread` returns a `Future`, `benchmark` is a function, `retry_on_exception` is deprecated and bounded, `rate_limit` raises `RateLimitExceeded`, and the logging decorators default to `DEBUG`. Python 3.8 and 3.9 are no longer supported. `from python_wrap.decorators import ...` still works. See the [migration guide](https://pythonwrap.readthedocs.io/en/latest/project/migration/).

## Contributing

Bug reports, ideas and pull requests are welcome. Read [CONTRIBUTING.md](https://github.com/LitePy/PythonWrap/blob/main/CONTRIBUTING.md) for the development setup and checks, and [SECURITY.md](https://github.com/LitePy/PythonWrap/blob/main/SECURITY.md) for reporting vulnerabilities.

## License

[MIT](https://github.com/LitePy/PythonWrap/blob/main/LICENSE)
