# run_in_thread

Start each call in a new thread and get a `concurrent.futures.Future` back. This lets you run blocking code without waiting for it.

```python
from python_wrap import run_in_thread

@run_in_thread
def add(a: int, b: int) -> int:
    return a + b

future = add(1, 2)
assert future.result() == 3    # waits; re-raises the function's exception, if any
```

## When to use it

- You want to fire off blocking work and collect the result later.
- You want a `Future` you can pass around, wait on with a timeout, or attach callbacks to.
- To await blocking code from `async` code, `asyncio.to_thread` is usually a better fit: `run_in_thread` returns a plain `Future`, not an awaitable.

## Behaviour

- `future.result()` is the equivalent of v0.1's `thread.join()`, plus the return value.
- `async def` functions run with `asyncio.run` in the new thread (their own event loop), and the wrapper itself is a plain function returning a `Future`.
- The caller's `contextvars` are copied into the thread.
- Options: `daemon=True` (do not keep the interpreter alive) and `name=` (thread name).

```python
import asyncio
from python_wrap import run_in_thread

@run_in_thread(daemon=True, name="worker")
async def fetch() -> str:
    await asyncio.sleep(0.01)
    return "done"

assert fetch().result() == "done"
```

## See also

- [Async helpers](async-helpers.md), [Bulkheads](bulkheads.md)
- [Async and methods](../../get-started/async-and-methods.md)
- [API reference](../../api/concurrency.md)
