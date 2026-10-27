# Hedge

Most calls to a replicated service are fast, but a few hit a slow replica, a GC pause, or a lost packet. A **hedged request** waits a little; if the call is still running, it fires a second, identical call and takes whichever answers first.

```python
import asyncio
from python_wrap.hedging import hedge

attempts = []

@hedge(0.05, max_extra=1)
async def get_price(sku: str) -> float:
    attempts.append(sku)
    if len(attempts) == 1:
        await asyncio.sleep(10)      # the first replica is stuck...
    return 9.99

async def main() -> None:
    assert await get_price("A1") == 9.99   # ...so the backup answers after ~50 ms
    assert len(attempts) == 2

asyncio.run(main())
```

## When to use it

- Reads against a replicated service where a small share of calls is much slower than the rest (tail latency).
- Idempotent lookups where an extra request costs little.
- Not for slow calls that should simply be limited: use [timeout](timeout.md).
- Not for failures that should be repeated after a pause: use [retry](retry.md).

!!! warning "Only for idempotent operations"
    A hedged function can run several times for one call, at the same time. Use it for reads and other operations that are safe to repeat. Never hedge payments, emails, counters, or anything else whose side effect must happen once.

## How it decides

1. Start the call.
2. If no attempt has finished after `delay` seconds, start another one (up to `max_extra` extra attempts in total, each `delay` after the previous one).
3. The first attempt to **succeed** wins; the rest are cancelled.
4. If an attempt fails with an exception matching `on` (default: any `Exception`) while there are attempts left, the next one starts **immediately**: there is no point waiting out the delay.
5. An exception that does not match `on` is raised at once (and the other attempts are cancelled).
6. If every attempt fails, the last exception is raised.

Pick `delay` near the latency percentile you want to cut, for example the p95 of the call. Then only about 5% of calls send an extra request.

```python
import asyncio
from python_wrap.hedging import hedge

calls = []

@hedge(10, max_extra=2, on=ConnectionError)
async def fetch() -> str:
    calls.append(1)
    if len(calls) < 3:
        raise ConnectionError("replica down")
    return "ok"

async def main() -> None:
    assert await fetch() == "ok"   # failures hedge immediately: no 10 s waits
    assert len(calls) == 3

asyncio.run(main())
```

## Async and methods

**Async: cancellation guarantees.** Each attempt is a task. When the wrapper returns or raises, every other attempt has been cancelled **and awaited**, so no tasks are left running. Cancelling the caller cancels all attempts. Errors raised by losing attempts while they are being cancelled are discarded. Context variables are copied into each attempt.

**Sync: attempts cannot be cancelled.** For a regular function each attempt runs in its own daemon thread and the caller blocks until one succeeds.

```python
from python_wrap.hedging import hedge

@hedge(0.01)
def lookup(key: str) -> str:
    return key.upper()

assert lookup("a") == "A"
```

`hedge` keeps the decorated function's signature and type, so it stacks with other decorators and works on methods.

## Testing

The delay before a backup attempt is a real timer. Neither `fast_mode()` nor `fake_clock()` changes it, because making it instant would hedge every call at once. Use tiny real delays in tests. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Sync attempts keep running"
    **Python threads cannot be cancelled.** Losing sync attempts keep running in the background until they finish, and their results are thrown away. Their side effects still happen, and they hold their resources (a connection, a lock) until done. Make sure the function has its own timeout, so a stuck attempt does not run forever.

`hedge` emits no events.

## See also

- [Timeout](timeout.md), [Retry](retry.md)
- [Singleflight](../caching/singleflight.md) to merge concurrent identical calls (the opposite problem)
- [API reference](../../api/hedging.md)
