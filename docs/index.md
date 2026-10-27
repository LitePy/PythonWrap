# python-wrap

**Everyday Python decorators that just work.** Retry, cache, time out, rate limit, validate, log and more: the small toolkit you end up adding to every project, in one typed, async-aware package with no dependencies.

```python
from python_wrap import retry, timeout, memoize

@memoize(ttl=60)
@retry(max_attempts=3, delay=0.5, backoff=2, on=ConnectionError)
@timeout(5)
def load_profile(user_id: int) -> dict:
    ...
```

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

Not sure which decorator you need? Start with [Choose a decorator](choosing.md).

## Principles

**Zero dependencies.** `dependencies = []` is a feature: python-wrap is safe to add to any project, including libraries.

**Typed.** The package ships `py.typed`, passes `mypy --strict`, and preserves signatures with `ParamSpec`, so your editor still knows what the decorated function takes and returns.

**Async is first class.** Async functions get `async def` wrappers (so `inspect.iscoroutinefunction` stays true), retries and rate limits wait with `asyncio` instead of blocking the loop, and caches de-duplicate concurrent awaits. See [Async support](async.md).

**It never configures logging.** Importing python-wrap does not touch the root logger. Decorators log to the `python_wrap` logger (or the `logger=` you give them), which carries a `NullHandler`, so nothing is printed until your application configures logging. See [Observability](guides/observability.md#configuring-logging).

**Failures are explicit.** The package raises its own exceptions (`TimeoutExpired`, `RateLimitExceeded`, `CircuitOpenError`, `ValidationError`, `ArgumentTypeError`), each also a subclass of the closest built-in, so `except TimeoutError` and `except ValueError` keep working.

**Honest about limits.** Some of these tools cannot do everything (a thread timeout cannot stop the work, for example). They are documented on one page: [Limitations](limitations.md).

## Where to go next

- [Choose a decorator](choosing.md): a table by problem.
- Guides: [Resilience](guides/resilience.md), [Caching](guides/caching.md), [Concurrency](guides/concurrency.md), [Observability](guides/observability.md), [Validation](guides/validation.md), [State safety](guides/state.md), [Developer tools](guides/devtools.md).
- [Stacking decorators](stacking.md): order matters.
- [Migrating from 0.1](migration.md): every breaking change, with before and after code.
