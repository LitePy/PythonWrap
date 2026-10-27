# Cached property

Like `functools.cached_property`, plus `ttl`, per-instance locking, `async def` getters and explicit invalidation.

```python
from python_wrap import cached_property, invalidate

class Report:
    loads = 0

    @cached_property(ttl=60)
    def totals(self) -> int:
        Report.loads += 1
        return 42

r = Report()
assert r.totals == 42 and r.totals == 42
assert Report.loads == 1

del r.totals                    # invalidate; the next access recomputes
invalidate(r, "totals")         # same, by name (no names: every property)
assert r.totals == 42 and Report.loads == 2
```

## When to use it

- An expensive attribute of one object, computed on first access.
- A method of a class with unhashable instances, where [`memoize`](memoize.md) would fail.
- Not for results that depend on arguments: use [`memoize`](memoize.md).

## Async and methods

An `async def` getter is awaited once; concurrent accesses share one task. Type checkers infer `Awaitable[T]` for these getters.

```python
import asyncio
from python_wrap import cached_property

class Client:
    calls = 0

    @cached_property
    async def profile(self) -> dict:
        Client.calls += 1
        await asyncio.sleep(0.01)
        return {"name": "ada"}

async def main() -> None:
    c = Client()
    a, b = await asyncio.gather(c.profile, c.profile)  # one shared task
    assert a == b == {"name": "ada"} and Client.calls == 1

asyncio.run(main())
```

## Gotchas

!!! warning "`__slots__` classes"
    The value is stored in the instance `__dict__`, so a `__slots__` class must list `"__dict__"`; otherwise access raises `TypeError`.

!!! note "Read-only, not copied"
    The attribute is read-only (assigning raises `AttributeError`); use `del` or `invalidate`. Copies and pickles start with an empty cache. Type checkers infer the getter's return type.

## See also

- [Memoize](memoize.md), [Once](once.md)
- [API reference](../../api/caching.md)
