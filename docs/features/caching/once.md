# Once

Run a function a single time; later calls return the first result and ignore their arguments.

```python
from python_wrap import once

@once
def setup() -> object:
    return object()

assert setup() is setup()
assert setup.called is True

setup.reset()          # forget the result; the next call runs again
assert setup.called is False
```

## When to use it

- Initialisation, setup or a config load that must happen one time.
- Not when the result depends on arguments: use [`memoize`](memoize.md).
- Not for a per-object attribute: use [`cached_property`](cached-property.md).

## Failures

If the first call raises, the function is not marked done and the next call tries again.

## Async and methods

`once` is thread-safe. For `async def` functions, concurrent first awaits share one run, even across threads that each run their own event loop: it runs once per process.

```python
import asyncio
from python_wrap import once

runs = []

@once
async def connect() -> str:
    runs.append(1)
    await asyncio.sleep(0.01)
    return "connection"

async def main() -> None:
    a, b = await asyncio.gather(connect(), connect())  # share one task
    assert a == b == "connection"
    assert runs == [1]

asyncio.run(main())
```

## Testing

While `python_wrap.testing.disable_caching()` is active, `once` runs the function on every call and stores nothing. Use `.reset()` to re-arm it between tests.

## See also

- [Memoize](memoize.md), [Cached property](cached-property.md)
- [API reference](../../api/caching.md)
