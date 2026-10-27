# Fallback and suppress

`fallback` returns a value, or the result of a handler, when the function raises. `suppress` swallows matching exceptions and returns `None`.

=== "Sync"

    ```python
    from python_wrap import fallback

    @fallback(0, on=KeyError)
    def stock(sku: str) -> int:
        return {"a": 5}[sku]

    assert stock("a") == 5
    assert stock("missing") == 0

    @fallback(handler=lambda exc: f"failed: {exc!r}")
    def risky() -> str:
        raise RuntimeError("boom")

    assert risky() == "failed: RuntimeError('boom')"
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import fallback

    @fallback(0, on=KeyError)
    async def stock(sku: str) -> int:
        return {"a": 5}[sku]

    assert asyncio.run(stock("missing")) == 0
    ```

## When to use it

- A missing optional value should become a default (a zero count, an empty list, a cached copy).
- A failure should degrade the response instead of failing the request.
- Use `suppress` for best-effort work such as cleanup or telemetry, where nobody needs the result.
- Not to hide bugs: narrow `on=` to the errors you expect.

## fallback

Give exactly one of `value` or `handler=`; giving both or neither raises `ValueError`. The exception is logged at WARNING with its traceback. For async functions the handler is still a regular function.

!!! tip
    Pass a fresh object with `handler=lambda exc: []` if the default is mutable, so callers do not share one list.

## suppress

Suppressed errors are logged at DEBUG, and the call returns `None`.

```python
from python_wrap import suppress

@suppress(KeyError)
def first(d: dict[str, int]) -> int:
    return d["x"]

assert first({"x": 1}) == 1
assert first({}) is None

@suppress  # bare form suppresses Exception
def best_effort() -> None:
    raise RuntimeError

assert best_effort() is None
```

Use `fallback` when you need a value other than `None`.

## Gotchas

!!! note "Position in a stack"
    `fallback` belongs outside [retry](retry.md) and the [circuit breaker](circuit-breaker.md), so it only answers after they have given up. A [policy](policies.md) puts it there for you.

## See also

- [Retry](retry.md), [Circuit breaker](circuit-breaker.md), [Policies](policies.md)
- [Errors](../../get-started/errors.md)
- [API reference](../../api/retry.md)
