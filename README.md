<div align="center">

# python-wrap

**The decorator toolkit you add to every Python project.**<br>
Retry, cache, rate limit, time out, validate, log and more. One import, sync and async, zero dependencies.

[![CI](https://github.com/LitePy/PythonWrap/actions/workflows/ci.yml/badge.svg)](https://github.com/LitePy/PythonWrap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/python-wrap?logo=pypi&logoColor=white)](https://pypi.org/project/python-wrap/)
[![Python](https://img.shields.io/pypi/pyversions/python-wrap?logo=python&logoColor=white)](https://pypi.org/project/python-wrap/)
[![License: MIT](https://img.shields.io/pypi/l/python-wrap)](LICENSE)
[![Docs](https://readthedocs.org/projects/python-wrap/badge/?version=latest)](https://python-wrap.readthedocs.io)
[![Typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-2a6db2)](https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict)

[**Documentation**](https://python-wrap.readthedocs.io) ·
[Migration guide](https://python-wrap.readthedocs.io/en/latest/migration/) ·
[Changelog](CHANGELOG.md)

</div>

## Why python-wrap

- **Zero dependencies.** Pure Python, nothing to audit or pin.
- **Typed.** Every decorator preserves the wrapped signature (`ParamSpec`), so editors and mypy still see the real parameters and return type. The package ships `py.typed` and is checked with `mypy --strict`.
- **Sync and async, one API.** The same decorator works on `def`, `async def` and methods. Async wrappers stay coroutine functions and never block the event loop.
- **Never configures your logging.** Importing python-wrap does not touch the root logger. Anything that logs takes `logger=` and `level=`; the package logger only has a `NullHandler`.
- **Thread-safe.** Caches, limiters, breakers and transactions are guarded for concurrent use.
- **Tested on Python 3.10 to 3.14, on Linux, macOS and Windows.**

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
from python_wrap import memoize, rate_limit, retry, timeout


@retry(max_attempts=4, delay=0.01, backoff=2.0, jitter=0.01, on=ConnectionError)
@memoize(ttl=60, maxsize=256)
@rate_limit(10, 1.0, block=True)
@timeout(5.0)
def fetch_user(user_id: int) -> dict[str, int]:
    # Your real HTTP call goes here.
    return {"id": user_id}


print(fetch_user(1))        # runs the function
print(fetch_user(1))        # served from the cache
print(fetch_user.cache_info())


@retry(max_attempts=3, delay=0.01, on=ConnectionError)
@timeout(2.0)
async def fetch_async(user_id: int) -> dict[str, int]:
    await asyncio.sleep(0.01)
    return {"id": user_id}


print(asyncio.run(fetch_async(2)))
```

Each retry attempt gets its own deadline, and the cache sits outside the rate limiter so hits cost nothing. The same decorators work unchanged on `async def`.

## What is in the box

Every decorator below is importable from `python_wrap`.

| Area | Decorator | What it does |
|---|---|---|
| **Resilience** | `retry` | Retry on failure with backoff, jitter, `max_delay`, `retry_if` and an `on_retry` hook |
| | `fallback` | Return a default value (or a handler's result) when the call raises |
| | `suppress` | Swallow chosen exceptions; the call returns `None` |
| | `circuit_breaker` / `CircuitBreaker` | Stop calling a failing dependency until it recovers; share one breaker across functions |
| | `timeout` | Raise `TimeoutExpired` when a call runs too long |
| **Caching** | `memoize` | LRU and TTL cache with `maxsize`, `typed`, custom `key`, `cache_info()` and `cache_clear()` |
| | `cache` | v0.1 name for `memoize(ttl=...)` |
| | `once` | Run a function a single time and reuse the result (thread-safe, resettable) |
| **Concurrency** | `rate_limit` | Sliding-window limit of N calls per period, per key if you want |
| | `throttle` | Run at most once per interval |
| | `debounce` | Run only after calls stop for a quiet period; `.cancel()` and `.flush()` |
| | `singleflight` | Concurrent calls with the same arguments share one execution |
| | `run_in_thread` | Run in a thread and get a `concurrent.futures.Future` |
| **Observability** | `log_args` / `log_return` | Log arguments (with secret redaction) and return values |
| | `trace` | Log the call stack as functions run |
| | `audit` | Emit an `AuditRecord` per call to a logger and/or your own sink |
| | `timing` | Log duration; `threshold=` and `callback=` for slow-call alerts |
| | `profile` | Run `cProfile` around a call |
| | `measure_memory` | Memory use per call via `tracemalloc` |
| | `benchmark` / `benchmark_async` | Plain functions that time a callable over several runs |
| **Validation** | `enforce_types` | Check arguments and return value against the function's own annotations |
| | `validate_args` | Per-parameter predicates with optional custom messages |
| | `check_type` | Explicit per-parameter type checks |
| **State** | `transactional` | Commit and rollback callbacks around a call |
| | `Transaction`, `current_transaction` | The transaction object, usable as a (async) context manager |
| | `revert_on_failure` | Restore an object's attributes if the call raises |
| **Dev tools** | `deprecate` | Emit a `DeprecationWarning` at the caller; works on functions and classes |
| | `mock_data` | Return fixed data instead of calling the function, for tests and local runs |
| | `compose` | Combine several decorators into one |

Exceptions live in `python_wrap.errors` and are also exported at the top level: `PythonWrapError`, `TimeoutExpired` (a `TimeoutError`), `RateLimitExceeded`, `CircuitOpenError`, `ValidationError` (a `ValueError`) and `ArgumentTypeError` (a `TypeError`).

## Highlights

### retry

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

The last exception is re-raised unchanged once attempts run out. `max_attempts=None` retries forever. Async functions are retried with `asyncio.sleep`.

Fail soft instead of raising with `fallback` and `suppress`:

```python
from python_wrap import fallback, suppress


@fallback({}, on=KeyError)
def load_settings() -> dict[str, str]:
    raise KeyError("settings.json")


@suppress(OSError)
def best_effort_cleanup() -> None:
    raise OSError("already gone")


print(load_settings())          # {}
print(best_effort_cleanup())    # None
```

### Circuit breaker

```python
from python_wrap import CircuitBreaker, CircuitOpenError, CircuitState

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
```

After `recovery_timeout` the breaker goes half-open and lets a trial call through; success closes it, failure re-opens it. Decorate several functions with the same `CircuitBreaker` to share state, or use `@circuit_breaker(...)` for a private one (reachable as `func.breaker`).

### memoize

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

Keys are built from the bound signature, lists, dicts and sets are converted automatically, and exceptions are never cached. Concurrent awaits of the same missing key in an `async def` share one in-flight task. On methods the cache keeps instances alive, so give it `maxsize` or `ttl`.

### rate_limit, throttle and debounce

```python
import time

from python_wrap import RateLimitExceeded, debounce, rate_limit, throttle


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


@throttle(0.1)              # at most once per 0.1 s, others get the last result
def refresh() -> float:
    return time.monotonic()


@debounce(0.05)             # only the last call of a burst runs
def save(text: str) -> None:
    print("saved", text)


for t in ("h", "he", "hel"):
    save(t)
time.sleep(0.15)            # prints: saved hel
```

`rate_limit(block=True)` waits instead of raising (and uses `await` for `async def`). The sliding window never lets a `2 * calls` burst through at a window edge.

### timeout

```python
import asyncio
import time

from python_wrap import TimeoutExpired, timeout


@timeout(0.05)
def slow() -> None:
    time.sleep(1)


@timeout(0.05)
async def slow_async() -> None:
    await asyncio.sleep(1)


for call in (slow, lambda: asyncio.run(slow_async())):
    try:
        call()
    except TimeoutError as exc:         # TimeoutExpired subclasses TimeoutError
        assert isinstance(exc, TimeoutExpired)
        print("timed out")
```

`async def` functions are cancelled with `asyncio.wait_for`. Sync functions are interrupted with `SIGALRM` on Unix when called from the main thread. Everywhere else (Windows, worker threads, `use_signals=False`) the call runs in a daemon thread and the caller stops waiting, but **the work itself cannot be stopped**; it finishes in the background and its result is discarded.

### enforce_types

```python
from python_wrap import ArgumentTypeError, enforce_types


@enforce_types
def total(prices: list[float], currency: str = "USD") -> str:
    return f"{sum(prices):.2f} {currency}"


print(total([1.5, 2.5]))
try:
    total([1.5, "2.5"])      # type: ignore[list-item]
except ArgumentTypeError as exc:
    print(exc)               # parameter 'prices' expected list[float], got list containing str at index 1
```

Checks come from the annotations you already wrote: generics, unions, `Literal`, `Annotated`, `NewType` and more. Annotations it does not understand are accepted, never rejected. For custom rules use `validate_args(x=lambda x: x > 0)` or `validate_args(name=(lambda s: s.strip() != "", "name must not be blank"))`.

### log_args with redaction

```python
import logging

from python_wrap import log_args

logging.basicConfig(level=logging.DEBUG, format="%(message)s")   # your app configures logging


@log_args(logger="myapp.auth")
def login(username: str, password: str, api_key: str = "") -> bool:
    return True


login("ann", "hunter2", api_key="sk-123")
# Calling login(username='ann', password='***', api_key='***')
```

Any parameter whose name contains `password`, `token`, `secret`, `api_key`, `authorization`, `cookie` and similar (see `DEFAULT_REDACT`) is masked. Pass `redact=("ssn",)` to choose your own fragments, `redact=()` to turn it off, and `max_length=` to truncate huge values. `audit` uses the same redaction and hands you structured `AuditRecord` objects.

### Transactions

```python
from python_wrap import Transaction, transactional

ledger = {"a": 100, "b": 0}


@transactional
def transfer(src: str, dst: str, amount: int, *, tx: Transaction) -> None:
    before = dict(ledger)
    tx.on_rollback(ledger.update, before)
    tx.on_commit(print, "transfer committed")
    ledger[src] -= amount
    ledger[dst] += amount
    if ledger[src] < 0:
        raise ValueError("insufficient funds")


transfer("a", "b", 30)          # commits
try:
    transfer("a", "b", 500)     # raises, rollback callbacks run
except ValueError:
    pass
print(ledger)                   # {'a': 70, 'b': 30}
```

A function with a `tx` parameter receives the transaction (pass it last or keyword-only); any other code can call `current_transaction()`. Callbacks may be `async` when the function is, and rollback callbacks run in reverse order. `Transaction` is also a plain `with` / `async with` context manager. For simple in-memory rollback, `@revert_on_failure` snapshots an object's attributes and restores them if the call raises.

## Stacking decorators

Decorators are applied bottom-up and run top-down, so order matters. Rules of thumb:

| Put this... | ...outside of this | Because |
|---|---|---|
| `retry` | `timeout`, `circuit_breaker`, `rate_limit` | Each attempt gets its own deadline, and a rejected attempt can be retried |
| `memoize` | `retry`, `rate_limit`, `timeout` | Cache hits skip everything; only misses pay |
| `circuit_breaker` | `timeout` | A timeout counts as a failure and can open the circuit |
| `fallback` | `retry`, `circuit_breaker` | Fall back only after retries are exhausted or the circuit is open |
| `log_args`, `timing`, `audit` | everything | They then observe what callers actually experience |
| `enforce_types`, `validate_args` | the rest | Reject bad input before doing any work |

A good default for a network call, outermost first:

```python
from python_wrap import circuit_breaker, compose, fallback, memoize, retry, timeout

resilient = compose(
    fallback(None, on=Exception),
    memoize(ttl=30),
    retry(max_attempts=3, delay=0.01, backoff=2),
    circuit_breaker(failure_threshold=5, recovery_timeout=30),
    timeout(5),
)


@resilient
def get_price(sku: str) -> float | None:
    return 9.99


print(get_price("abc"))
```

`compose(a, b, c)(f)` is exactly `a(b(c(f)))`, so you can name a stack once and reuse it. Note that `rate_limit`, `throttle` and `debounce` keep one state per decorated function, which for methods is shared by all instances (use `rate_limit(key=...)` to split it).

## How it compares

python-wrap is a broad toolkit; the others are deeper in one area. Use them when that one area is all you need and you want the specialist.

| Library | Use it instead when... | python-wrap's angle |
|---|---|---|
| [tenacity](https://github.com/jd/tenacity) | You need very rich retry policies: composable stop/wait strategies, retry statistics, per-call overrides | `retry` covers the common cases (backoff, jitter, predicates, hooks, async) in one decorator with a flat signature |
| [backoff](https://github.com/litl/backoff) | You already use it and are happy. Note that the repository was archived in 2025, so it no longer receives fixes | A maintained, typed retry with the same core ideas, plus everything else in this table |
| [cachetools](https://github.com/tkem/cachetools) | You need other eviction policies (LFU, FIFO, RR), explicit cache objects, or custom key and lock control | `memoize` gives LRU + TTL with `cache_info()`, async in-flight dedupe and signature-bound keys |
| [functools](https://docs.python.org/3/library/functools.html) | A plain `lru_cache` or `cache` on a sync function with hashable arguments is enough. It is in the standard library, so prefer it for that | Adds TTL, async support, unhashable argument handling and `cache_delete` |
| [pybreaker](https://github.com/danielfm/pybreaker) | You need listeners or circuit state shared across processes (Redis storage) | A typed `CircuitBreaker` with async support and no extra dependency |
| [ratelimit](https://github.com/tomasbasham/ratelimit) | You only need a simple fixed-window limiter | Sliding window (no boundary bursts), per-key buckets, `block=True`, async |

If you want all of these behaviours behind one consistent, typed, dependency-free API, python-wrap is the shorter path. If you only ever need one of them at full depth, reach for the specialist.

## Upgrading from 0.1

0.2.0 fixes several decorators that did not do what their names promised (`transactional`, `revert_on_failure`, async `timing`) and makes a few breaking changes: `run_in_thread` returns a `Future`, `benchmark` is a function, `retry_on_exception` is deprecated, `rate_limit` raises `RateLimitExceeded`, and the logging decorators default to `DEBUG`. Python 3.8 and 3.9 are no longer supported. `from python_wrap.decorators import ...` still works.

See the [changelog](CHANGELOG.md) for the one-line migration of each change and the [migration guide](https://python-wrap.readthedocs.io/en/latest/migration/) for details.

## Contributing

Bug reports, ideas and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and checks, and [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

[MIT](LICENSE)
