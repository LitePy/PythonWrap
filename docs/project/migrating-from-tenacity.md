# Migrating from tenacity

python-wrap's `retry` covers the common tenacity patterns with one decorator and plain keyword arguments instead of composable strategy objects. This page maps every tenacity concept to its python-wrap equivalent and lists the behaviour differences to check. Much of it can be done mechanically with the [migration tool](migrate-tool.md).

## At a glance

=== "tenacity"

    <!-- skip -->
    ```python
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, max=30),
        retry=retry_if_exception_type(ConnectionError),
        reraise=True,
    )
    def fetch() -> str: ...
    ```

=== "python-wrap"

    ```python
    from python_wrap import retry

    @retry(on=ConnectionError, max_attempts=5, delay=1, backoff=2, max_delay=30)
    def fetch() -> str:
        return "ok"

    assert fetch() == "ok"
    ```

## Stop conditions

| tenacity | python-wrap |
| --- | --- |
| `stop_after_attempt(n)` | `max_attempts=n` (total calls, same meaning) |
| `stop_after_delay(s)` | `max_time=s` |
| `stop_after_attempt(n) \| stop_after_delay(s)` | `max_attempts=n, max_time=s` (whichever comes first) |
| `stop_never` or no `stop=` | `max_attempts=None` |
| `stop_before_delay(s)` | `max_time=s` (python-wrap already refuses a wait that would overrun the budget) |
| `stop_any` / `stop_all` with other shapes | no equivalent; use `retry_if=` to stop on a condition |

`stop=` is **never** unlimited by default in python-wrap: omitted `max_attempts` means 3. Tenacity's default is to retry forever, so say `max_attempts=None` when you rely on that.

## Wait strategies

| tenacity | python-wrap |
| --- | --- |
| `wait_fixed(s)` | `delay=s, backoff=1` |
| `wait_none()` / no `wait=` | `delay=0, backoff=1` (python-wrap's default `delay` is `1.0`, so set it) |
| `wait_exponential(multiplier=m, max=b, exp_base=e)` | `delay=m, backoff=e, max_delay=b` |
| `wait_exponential(..., min=a)` | `delay=max(m, a)`; see the note below |
| `wait_random(a, b)` | `delay=a, backoff=1, jitter=b - a` |
| `wait_fixed(s) + wait_random(0, j)` | `delay=s, backoff=1, jitter=j` |
| `wait_exponential_jitter(initial, max, exp_base, jitter)` | `delay=initial, backoff=exp_base, max_delay=max, jitter=jitter` |
| `wait_random_exponential(...)` (full jitter) | no exact equivalent; use `backoff=` with `jitter=` as the closest shape |
| `wait_incrementing`, `wait_chain` | `delays=[1, 2, 5, 10]` (an explicit schedule; the last value repeats) |
| a server-provided wait | `delay_from=retry_after_header` |

**`min=`.** Tenacity clamps every wait to at least `min`. python-wrap starts at `delay` and multiplies from there, so `wait_exponential(multiplier=1, min=4)` (tenacity: 4, 4, 4, 8, ...) becomes `delay=4, backoff=2` (4, 8, 16, ...). If you need the exact tenacity schedule, spell it out with `delays=[4, 4, 4, 8, 16]`.

```python
from python_wrap import retry

attempts = []

@retry(max_attempts=4, delays=[0, 0, 0], on=ValueError)
def flaky() -> int:
    attempts.append(1)
    if len(attempts) < 3:
        raise ValueError("not yet")
    return len(attempts)

assert flaky() == 3
```

## Retry conditions

| tenacity | python-wrap |
| --- | --- |
| `retry_if_exception_type(X)` | `on=X` (a type or a tuple of types) |
| `retry_if_exception(pred)` | `retry_if=pred` (called with the exception) |
| `retry_if_exception_type(X) & retry_if_exception(pred)` | `on=X, retry_if=pred` |
| `retry_if_result(pred)` | `retry_if_result=pred`, plus `on=()` if exceptions must not be retried |
| `retry_if_exception_type(X) \| retry_if_result(pred)` | `on=X, retry_if_result=pred` |
| `retry_if_not_exception_type(X)` | `retry_if=lambda e: not isinstance(e, X)` |
| `retry_if_not_result(pred)` | `retry_if_result=lambda r: not pred(r)` |
| `retry_if_exception_message(...)` | `retry_if=lambda e: "text" in str(e)` |
| HTTP status codes | `retry_if_result=http_status(429, 503)` |

`CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are never retried, even if `on=` lists them. Tenacity retries them if you tell it to.

```python
from python_wrap import http_status, retry

class Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

codes = iter([503, 503, 200])

@retry(max_attempts=5, delay=0, on=(), retry_if_result=http_status(429, 503))
def call() -> Response:
    return Response(next(codes))

assert call().status_code == 200
```

## Hooks

| tenacity | python-wrap |
| --- | --- |
| `before_sleep=f` | `on_retry=f`; `f` receives a `RetryState`, not tenacity's `RetryCallState` |
| `before_sleep=before_sleep_log(logger, level)` | `logger=logger, log_level=level` (python-wrap logs each retry by default at `WARNING`) |
| `after=f` / `before=f` | no hook; use `on_retry=` or events (`retry.attempt_failed`, `retry.succeeded`) |
| `retry_error_callback=f` | `fallback(...)` around the function, or catch the exception |
| `retry_error_cls=` | not needed: the original exception is re-raised |
| `sleep=` | `python_wrap.testing.fake_clock()` / `fast_mode()` in tests |

The tool does not rewrite hooks because the callback signature changes. Port them by hand:

=== "tenacity"

    <!-- skip -->
    ```python
    def log_retry(retry_state):
        print(retry_state.attempt_number, retry_state.outcome.exception())

    @retry(stop=stop_after_attempt(3), before_sleep=log_retry)
    def work(): ...
    ```

=== "python-wrap"

    ```python
    from python_wrap import RetryState, retry

    seen: list[int] = []

    def log_retry(state: RetryState) -> None:
        seen.append(state.attempt)   # also: state.exception, state.next_delay, state.elapsed

    calls = []

    @retry(max_attempts=3, delay=0, on_retry=log_retry)
    def work() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("boom")
        return "done"

    assert work() == "done"
    assert seen == [1, 2]
    ```

## Statistics

| tenacity | python-wrap |
| --- | --- |
| `f.retry.statistics["attempt_number"]` | `retry_stats(f).attempts_total` (all calls); per-call number is `RetryState.attempt` |
| `f.retry.statistics["idle_for"]` | `retry_stats(f).total_sleep` |
| `f.retry.statistics` (dict) | `retry_stats(f).snapshot()` |

```python
from python_wrap import retry, retry_stats

@retry(max_attempts=3, delay=0)
def sometimes(state: list[int] = []) -> str:
    state.append(1)
    if len(state) < 2:
        raise OSError
    return "ok"

sometimes()
stats = retry_stats(sometimes)
assert stats is not None and stats.retries == 1
print(stats.snapshot())
```

## Retrying / AsyncRetrying iterators

`for attempt in Retrying(...)` and `async for attempt in AsyncRetrying(...)` become `retrying(...)`, with `with attempt:` around the risky block. Async code uses `async for`.

=== "tenacity"

    <!-- skip -->
    ```python
    from tenacity import Retrying, stop_after_attempt

    for attempt in Retrying(stop=stop_after_attempt(3)):
        with attempt:
            do_work()
    ```

=== "python-wrap"

    ```python
    from python_wrap import retrying

    calls = []
    for attempt in retrying(max_attempts=3, delay=0):
        with attempt:
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError
    assert len(calls) == 3
    ```

=== "python-wrap (async)"

    ```python
    import asyncio
    from python_wrap import retrying

    async def main() -> int:
        calls = 0
        async for attempt in retrying(max_attempts=3, delay=0):
            with attempt:
                calls += 1
                if calls < 3:
                    raise ConnectionError
        return calls

    assert asyncio.run(main()) == 3
    ```

`@retry` already handles `async def` functions, so `AsyncRetrying(...).wraps(fn)` and `tenacity.retry` on a coroutine both become a plain `@retry` on the coroutine. The `Retrying` object itself (`retry_object(func, ...)`, `.wraps`, `.copy`, `with_options`) has no equivalent; build the decorator once and reuse it, or use `python_wrap.policy`.

## Budgets across a whole service

Tenacity has no shared retry limit. python-wrap's `RetryBudget` caps retries as a fraction of traffic, so a failing dependency is not hit `max_attempts` times per call from every worker:

```python
from python_wrap import RetryBudget, retry

budget = RetryBudget(ratio=0.1, min_per_second=10)

@retry(max_attempts=3, delay=0, budget=budget)
def call() -> str:
    return "ok"

assert call() == "ok"
assert budget.stats.requests == 1
```

For `Retry-After` headers use `delay_from=retry_after_header`:

```python
from python_wrap import retry, retry_after_header

class Throttled(Exception):
    retry_after = 0

n = []

@retry(max_attempts=2, delay=1.0, on=Throttled, delay_from=retry_after_header)
def limited() -> str:
    n.append(1)
    if len(n) == 1:
        raise Throttled
    return "ok"

assert limited() == "ok"
```

## Behavioural differences to check

1. **The exception type after giving up.** Tenacity raises `RetryError` unless `reraise=True`. python-wrap always re-raises the original exception with its traceback. Remove `except RetryError` handlers (and `reraise=True`, which is the default behaviour).
2. **Returned values on giving up.** With `retry_if_result`, tenacity raises `RetryError` when attempts run out; python-wrap **returns the last rejected value**. Check it if it matters.
3. **Defaults.** Tenacity: retry forever, no wait. python-wrap: 3 attempts, 1 second delay. Both are explicit in migrated code.
4. **`retry_if_result` alone.** Tenacity then does not retry exceptions. python-wrap's `on=` defaults to `Exception`, so pass `on=()` for the same behaviour (the tool does this).
5. **`stop_after_delay`.** Tenacity checks the budget after an attempt; python-wrap's `max_time` re-raises when the *next wait* would run past it, so it never sleeps into a budget it cannot use. Attempts can end slightly earlier.
6. **Cancellation.** `CancelledError`, `KeyboardInterrupt` and `SystemExit` always propagate.
7. **Deadlines.** Inside a `python_wrap.deadline(...)` block a retry never sleeps past the time remaining.
8. **Hook failures.** An exception raised by `on_retry` is logged and swallowed; it does not abort the retry.
9. **Jitter.** Tenacity's `wait_random_exponential` randomises the whole wait; python-wrap's `jitter` adds up to that many seconds on top of the computed wait.

## What the tool does and does not do

`python -m python_wrap migrate` rewrites the decorator shapes in the tables above, fixes the imports, and reports everything else with a line and column. It never touches `Retrying`/`AsyncRetrying` loops, hooks, `retry_error_callback`, custom strategies or code that reads `f.retry.statistics`. See [the migration tool](migrate-tool.md).
