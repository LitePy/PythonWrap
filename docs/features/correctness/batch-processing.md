# Batch processing and checkpoints

`process_batches` runs a handler for each input, records an outcome, and keeps going
when an ordinary item exception occurs. A checkpoint records the next input index.

```python
from python_wrap import MemoryCheckpoint, process_batches

checkpoint = MemoryCheckpoint()
progress = []

def process(value):
    if value == 2:
        raise ValueError("invalid value")
    return value * 2

result = process_batches([1, 2, 3], process, chunk_size=2, checkpoint=checkpoint,
                         on_progress=lambda done, index: progress.append((done, index)))
assert [outcome.result for outcome in result.succeeded] == [2, 6]
assert result.failed[0].item == 2
assert result.to_rows(reason_column="import_failure_reason") == [
    {"item": 2, "status": "failed", "import_failure_reason": "invalid value"}
]
assert checkpoint.load() == 3
assert progress == [(2, 2), (3, 3)]
assert not process_batches([1, 2, 3], process, checkpoint=checkpoint).outcomes
```

`Outcome` contains the input index, item, classification, result and original exception.
Use `classify(exc)` to choose `"failed"`, `"parked"`, `"skipped"`, or `"conflict"`.
Successful items use `"succeeded"`. `BatchResult.failed` includes all non-success
classifications. `to_rows()` turns failed items into report rows; mapping items retain
their original fields, so the application controls what data is exported.

The callback `on_progress(processed_this_call, next_input_index)` runs at chunk checkpoints
and after the final partial chunk. `RedisCheckpoint(client, key)` persists the same
next-index contract. It does not lock the checkpoint: use one owner per input, with a
lease when several workers could process it.

## Async processing

`aprocess_batches` accepts an async handler and uses the same outcome and checkpoint
contracts. Cancellation propagates; it is not classified as a failed item. Synchronous
checkpoint I/O, synchronous handlers and synchronous progress callbacks run in worker
threads. Async handlers and callbacks are awaited. `Outcome[T, R]` and `BatchResult[T, R]`
retain the input and result types for type checkers.

```python
import asyncio
from python_wrap import aprocess_batches

async def process(item):
    return item + 1

result = asyncio.run(aprocess_batches([1, 2], process))
assert [outcome.result for outcome in result.succeeded] == [2, 3]
```

!!! note "Resumption can replay a chunk"
    A checkpoint advances after a chunk, including recorded failures. A crash between
    an item's external effect and the checkpoint can replay that item. Input order must
    remain stable, handlers must be replay-safe, and persisted checkpoints must be unique
    to that input. This is not an exactly-once transaction. The returned outcomes use
    memory proportional to processed items; `chunk_size` bounds checkpoint frequency,
    not the total result size.

See [processing API](../../api/processing.md).

Synchronous handlers and checkpoint writes in `aprocess_batches` run in a worker thread.
Cancellation waits for the current synchronous operation to settle before returning,
including repeated cancellation, so an enclosing lease cannot be released while a write
is still in progress. Configure timeouts on database/HTTP clients: Python cannot forcibly
stop that worker thread. Async handlers keep normal coroutine cancellation semantics.
