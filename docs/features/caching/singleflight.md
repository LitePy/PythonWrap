# Singleflight

While a call for a given key is running, further calls with the same key wait and receive **the same result or exception**. Once it finishes, the next call runs fresh.

This de-duplicates work in flight; it is not a cache. Use it to stop a thundering herd of identical requests (a cache miss that every request tries to fill).

=== "Sync"

    ```python
    import time
    from concurrent.futures import ThreadPoolExecutor
    from python_wrap import singleflight

    calls = []

    @singleflight
    def load(path: str) -> str:
        calls.append(path)
        time.sleep(0.03)
        return f"contents of {path}"

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(load, ["a.txt"] * 3))

    assert results == ["contents of a.txt"] * 3
    assert len(calls) == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import singleflight

    calls = []

    @singleflight
    async def load(path: str) -> str:
        calls.append(path)
        await asyncio.sleep(0.01)
        return f"contents of {path}"

    async def main() -> None:
        results = await asyncio.gather(*(load("a.txt") for _ in range(3)))
        assert results == ["contents of a.txt"] * 3
        assert calls == ["a.txt"]

    asyncio.run(main())
    ```

## When to use it

- Many callers ask for the same expensive thing at the same moment.
- Stacked under [`memoize`](memoize.md), so a cache miss is filled once rather than by every concurrent caller.
- Not to remember results after the call finishes: use [`memoize`](memoize.md).
- Not to merge different items into one bulk call: use [`batch_calls`](batching.md).

## Keys

The default key is the positional arguments plus sorted keyword arguments (including `self` for methods), which must be hashable. Pass `key=` to choose what counts as "the same call", for example `key=lambda path, **_: path`.

## Async and methods

Sync functions de-duplicate across threads. Async functions de-duplicate across tasks on the same event loop; the work runs in a shared task, so cancelling one waiting caller does not cancel it for the others.

## Gotchas

!!! warning "State on methods"
    On a **method**, `singleflight` state belongs to the decorated function and is shared by every instance. The default key includes `self`, which separates instances; if you pass `key=`, include whatever must keep instances apart. See [Limitations](../../project/limitations.md).

## See also

- [Memoize](memoize.md), [Batching](batching.md)
- [API reference](../../api/concurrency.md)
