# Buffered background writes

`batch_calls` makes each caller await its result. `buffered` accepts records for a
background batch writer, allowing the caller to continue after submission.

```python
from python_wrap import buffered

batches = []

@buffered(max_size=2, flush_interval=0.01, max_queue=100)
def write_rows(rows):
    batches.append(rows)

assert write_rows.submit({"id": 1})
assert write_rows.submit({"id": 2})
assert write_rows.close(timeout=2)
assert sum(len(batch) for batch in batches) == 2
assert write_rows.stats().written == 2
```

A thread starts lazily on first submission. Calling `write_rows(batch)` directly calls
the underlying handler immediately; `.submit(record)` is the queued API. `flush(timeout)`
dispatches waiting batches immediately, waits for accepted writes, and returns False
on timeout. `close(timeout)` stops accepting
new records and drains the queue. Use a context manager when the writer has a clear
lifetime; process exit only gets the bounded `drain_on_exit` allowance.

## Bounds and failure behavior

| Option/control | Behavior |
| --- | --- |
| `max_size` | Maximum records passed to a handler at once. |
| `flush_interval` | Maximum coalescing interval once a batch begins. |
| `max_queue` | Bound on waiting records, excluding the active batch. |
| `on_full="drop"` | Reject the new record and return False. |
| `on_full="drop_oldest"` | Drop the oldest waiting record to accept the new one. |
| `on_full="block"` | Wait for space, optionally bounded by `.submit(..., timeout=...)`. |
| `on_error="continue"` | Count and log a failed batch; process later batches. |
| `on_error="disable"` | Stop accepting records and discard waiting records after a batch failure. |
| `.disable(reason)` | Stop accepting new records; accepted records still drain. |

`stats()` returns queued/pending, written, dropped, failed batch/record counts, and whether
new submissions are enabled. Queued/pending includes the active batch. Handler failures
are not automatically retried; apply `retry` to the batch handler if replay is safe.
Submission success means accepted into memory, not durably committed. Use a durable
queue for work that must survive process termination.

## Async writer

An `async def` handler produces `AsyncBuffered`. Its submission and lifecycle methods
are awaited, and it belongs to the loop where it first runs.

```python
import asyncio
from python_wrap import buffered

async def main():
    rows = []

    @buffered(max_size=2, flush_interval=0.01)
    async def write(batch):
        rows.extend(batch)

    async with write:
        assert await write.submit(1)
        assert await write.submit(2)
        assert await write.flush(timeout=2)
    assert rows == [1, 2]

asyncio.run(main())
```

Close async writers before stopping their loop. Async submission, flush and close must
use the owning event loop. Cancelling a flush/close waiter does not cancel the writer;
cancelling the writer task itself disables it, counts its active batch as failed and drops
waiting records. A writer cannot flush itself from its own handler. After fork, inherited pending records are discarded to avoid duplicate
writes, and a child may lazily start a new writer. Start application writers in the
worker lifecycle rather than at module import.

On a method, descriptor binding creates a separate queue for each instance or class.
Such owners need a writable `__dict__`; close their writers as part of owner shutdown.

See [buffering API](../../api/buffering.md).
