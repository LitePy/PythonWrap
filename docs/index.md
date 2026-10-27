---
title: python-wrap
hide:
  - navigation
  - toc
---

<div class="pw-hero" markdown>

<img class="pw-logo" src="assets/logo-mark.svg" alt="">

# The toolkit you add to every Python project

<p class="pw-tagline">Retry (with <code>Retry-After</code>), circuit breaker, timeout, cache, rate limit,
bulkhead, batching, async helpers, metrics and test helpers. One import, sync and async,
fully typed, zero dependencies.</p>

[Get started](get-started/quickstart.md){ .md-button .md-button--primary }
[Choose a decorator](get-started/choosing.md){ .md-button }
[View on GitHub](https://github.com/LitePy/PythonWrap){ .md-button }

</div>

## One decorator, the whole policy

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

`policy(...)` applies the protections in the [recommended order](get-started/stacking.md) whatever order you list them in.

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

Not sure which decorator you need? Start with [Choose a decorator](get-started/choosing.md).

## Principles

<div class="grid cards" markdown>

-   :material-package-variant-closed:{ .lg .middle } __Zero dependencies__

    ---

    `dependencies = []` is a feature: python-wrap is safe to add to any project, including libraries.

-   :material-language-python:{ .lg .middle } __Typed__

    ---

    Ships `py.typed`, passes `mypy --strict`, and preserves signatures with `ParamSpec`, so your editor still knows what the decorated function takes and returns.

-   :material-lightning-bolt:{ .lg .middle } __Async is first class__

    ---

    Async functions get `async def` wrappers, waits use `asyncio` instead of blocking the loop, and caches share concurrent awaits. See [Async support](get-started/async-and-methods.md).

-   :material-text-box-search-outline:{ .lg .middle } __Never configures logging__

    ---

    Importing python-wrap does not touch the root logger. Decorators log to the `python_wrap` logger, which has a `NullHandler`. See [Observability](features/observability/logging.md#configuring-logging).

-   :material-chart-line:{ .lg .middle } __Observable and testable__

    ---

    `count_calls` and one [event hook](features/observability/metrics.md) cover retries, caches, circuits and limiters; [`python_wrap.testing`](testing/index.md) makes tests instant. Overhead numbers: [Performance](project/performance.md).

-   :material-alert-octagon-outline:{ .lg .middle } __Failures are explicit__

    ---

    Every error subclasses `PythonWrapError`, and `TimeoutExpired`, `ValidationError` and `ArgumentTypeError` also subclass the matching built-in, so `except TimeoutError` keeps working. See [Errors](get-started/errors.md).

</div>

Some of these tools cannot do everything (a thread timeout cannot stop the work, for example). Every such limit is on one page: [Limitations](project/limitations.md).

## Where to go next

<div class="grid cards" markdown>

-   :material-rocket-launch-outline:{ .lg .middle } __[Quickstart](get-started/quickstart.md)__

    ---

    Build a resilient client step by step: retry, timeout, cache, circuit breaker, policy, metrics and fast tests.

-   :material-compass-outline:{ .lg .middle } __[Choose a decorator](get-started/choosing.md)__

    ---

    A table by problem: what to reach for, and what to combine it with.

-   :material-view-grid-outline:{ .lg .middle } __[Features](features/index.md)__

    ---

    One page per feature: resilience, caching and batching, concurrency and limits, observability, correctness.

-   :material-test-tube:{ .lg .middle } __[Testing](testing/index.md)__

    ---

    `fast_mode`, a fake clock, spies and `eventually`: instant, deterministic tests.

-   :material-chef-hat:{ .lg .middle } __[Cookbook](cookbook/index.md)__

    ---

    Complete recipes for HTTP clients, crawlers, FastAPI, Django, CLIs and tests.

-   :material-swap-horizontal:{ .lg .middle } __[Migrating from 0.1](project/migration.md)__

    ---

    Every breaking change, with before and after code.

</div>
