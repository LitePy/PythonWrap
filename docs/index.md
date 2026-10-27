# python-wrap

**The resilience and concurrency toolkit you add to every project.** Retry (with `Retry-After`), circuit breaker, timeout, cache, rate limit, bulkhead, batching, async helpers, metrics and test helpers, in one typed, async-aware package with no dependencies.

```python
from python_wrap import CircuitBreaker, memoize, policy, retry, retry_after_header, timeout

http = policy(
    cache=memoize(ttl=60),
    breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30),
    retry=retry(max_attempts=3, delay=0.5, backoff=2, on=ConnectionError, delay_from=retry_after_header),
    timeout=timeout(5),
)

@http
def load_profile(user_id: int) -> dict:
    ...
```

`policy(...)` applies the protections in the [recommended order](stacking.md) whatever order you list them in.

## Install

=== "pip"

    ```bash
    pip install python-wrap
    ```

=== "uv"

    ```bash
    uv add python-wrap
    ```

Python 3.10 or newer. Linux, macOS and Windows.

## A 30-second tour

Every decorator works on plain functions, `async def` functions and methods, and keeps the wrapped function's signature for your type checker.

=== "Sync"

    ```python
    from python_wrap import retry, fallback, memoize

    attempts = []

    @retry(max_attempts=3, delay=0.01)
    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("try again")
        return "ok"

    assert flaky() == "ok" and len(attempts) == 3

    @fallback("unknown", on=KeyError)
    def lookup(key: str) -> str:
        return {"a": "alpha"}[key]

    assert lookup("a") == "alpha"
    assert lookup("zzz") == "unknown"

    @memoize(maxsize=128, ttl=60)
    def square(n: int) -> int:
        return n * n

    square(4)
    square(n=4)  # same cache entry as square(4)
    assert square.cache_info().hits == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import retry, fallback, memoize

    attempts = []

    @retry(max_attempts=3, delay=0.01)
    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("try again")
        return "ok"

    @fallback("unknown", on=KeyError)
    async def lookup(key: str) -> str:
        return {"a": "alpha"}[key]

    @memoize(ttl=60)
    async def square(n: int) -> int:
        return n * n

    async def main() -> None:
        assert await flaky() == "ok" and len(attempts) == 3
        assert await lookup("zzz") == "unknown"
        assert await asyncio.gather(square(4), square(4)) == [16, 16]

    asyncio.run(main())
    ```

More of the toolkit, in a few lines each:

=== "Limits and async"

    ```python
    import asyncio
    from python_wrap import Bulkhead, gather_limit, rate_limit

    db = Bulkhead(2, max_waiting=10, name="db")   # at most 2 concurrent calls

    @db
    @rate_limit(100, 1.0, block=True)             # at most 100 calls a second
    async def fetch(n: int) -> int:
        await asyncio.sleep(0)
        return n

    async def main() -> None:
        results = await gather_limit(*(fetch(i) for i in range(20)), limit=5)
        assert results == list(range(20))

    asyncio.run(main())
    ```

=== "Batching"

    ```python
    import asyncio
    from python_wrap import batch_calls

    calls: list[list[int]] = []

    @batch_calls(max_size=100, max_wait=0.005)    # many single lookups, one bulk call
    async def get_user(ids: list[int]) -> dict[int, str]:
        calls.append(ids)
        return {i: f"user{i}" for i in ids}

    async def main() -> None:
        assert await asyncio.gather(get_user(1), get_user(2)) == ["user1", "user2"]
        assert calls == [[1, 2]]

    asyncio.run(main())
    ```

=== "Fast tests"

    ```python
    from python_wrap import retry
    from python_wrap.testing import fast_mode

    calls: list[int] = []

    @retry(max_attempts=4, delay=30, on=ConnectionError)
    def flaky() -> str:
        calls.append(1)
        if len(calls) < 4:
            raise ConnectionError
        return "ok"

    with fast_mode():                  # waits are instant, retries still run
        assert flaky() == "ok"
    ```

Not sure which decorator you need? Start with [Choose a decorator](choosing.md).

## Principles

**Zero dependencies.** `dependencies = []` is a feature: python-wrap is safe to add to any project, including libraries.

**Typed.** The package ships `py.typed`, passes `mypy --strict`, and preserves signatures with `ParamSpec`, so your editor still knows what the decorated function takes and returns.

**Async is first class.** Async functions get `async def` wrappers (so `inspect.iscoroutinefunction` stays true), retries and rate limits wait with `asyncio` instead of blocking the loop, and caches de-duplicate concurrent awaits. See [Async support](async.md).

**It never configures logging.** Importing python-wrap does not touch the root logger. Decorators log to the `python_wrap` logger (or the `logger=` you give them), which carries a `NullHandler`, so nothing is printed until your application configures logging. See [Observability](guides/observability.md#configuring-logging).

**Observable and testable.** `count_calls` and one [event hook](guides/metrics.md) cover retries, caches, circuits and limiters, and [`python_wrap.testing`](testing.md) makes tests instant and deterministic. See [Performance](performance.md) for overhead numbers.

**Failures are explicit.** The package raises its own exceptions (`TimeoutExpired`, `RateLimitExceeded`, `CircuitOpenError`, `BulkheadFull`, `BatchError`, `ValidationError`, `ArgumentTypeError`), each also a subclass of the closest built-in, so `except TimeoutError` and `except ValueError` keep working.

**Honest about limits.** Some of these tools cannot do everything (a thread timeout cannot stop the work, for example). They are documented on one page: [Limitations](limitations.md).

## Where to go next

- [Choose a decorator](choosing.md): a table by problem.
- Guides: [Resilience](guides/resilience.md), [Caching](guides/caching.md), [Concurrency](guides/concurrency.md), [Async toolkit](guides/async-toolkit.md), [Batching and hedging](guides/batching.md), [Observability](guides/observability.md), [Metrics and events](guides/metrics.md), [Validation](guides/validation.md), [State safety](guides/state.md), [Policies](guides/policies.md), [Developer tools](guides/devtools.md), [Configuration](guides/configuration.md).
- [Cookbook](cookbook/index.md): complete recipes for HTTP clients, crawlers, FastAPI, Django, CLIs and tests.
- [Testing](testing.md) and [Performance](performance.md).
- [Stacking decorators](stacking.md): order matters.
- [Migrating from 0.1](migration.md): every breaking change, with before and after code.
