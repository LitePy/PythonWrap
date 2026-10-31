# Graceful shutdown

Stop background work, flush what is buffered and let in-flight calls finish, in one ordered pass with one time budget. Register the pieces once; call `drain()` from whatever your platform runs on shutdown.

```python
from python_wrap.lifecycle import ShutdownManager

shutdown = ShutdownManager()  # python_wrap.lifecycle.shutdown is a ready-made shared one
order: list[str] = []

shutdown.register(lambda: order.append("stop jobs"), name="jobs", priority=0)
shutdown.register(lambda: order.append("close db"), name="db", priority=20)

report = shutdown.drain(timeout=10)
assert report.ok
assert order == ["stop jobs", "close db"]
assert shutdown.drain() is report  # draining twice is a no-op
```

## When to use it

- A service with periodic tasks, buffered writers, batchers or debouncers that would lose data on `SIGTERM`.
- You want one place that decides the order (producers before consumers) and bounds the total time.
- You want a record of what did not finish, instead of an exception that hides the rest of the cleanup.

## What `register` understands

| You register | Drain does |
| --- | --- |
| `Periodic` / `AsyncPeriodic` | Stops the loop; no new run starts. |
| `Buffered` / `AsyncBuffered` | Closes it: refuses new records, writes the queued ones. |
| a `batch_calls` function, a `debounce`d function | Flushes pending work. |
| `Bulkhead`, `AdaptiveLimiter` | Waits until nothing is running or waiting. |
| any callable, sync or `async def` | Calls it (in a worker thread for sync drains, so a hang cannot block the budget). |

`register(target, *, name=None, priority=0)` returns `target`, so it can wrap a constructor. Lower `priority` runs first; ties run in registration order. A common layout: periodic tasks `0`, bulkheads `5` (let in-flight work finish), buffers `10`, connection pools `20`.

```python
import time
from python_wrap.buffering import buffered
from python_wrap.lifecycle import ShutdownManager
from python_wrap.periodic import periodic

written: list[object] = []


@buffered(max_size=100, flush_interval=60)
def sink(batch: list[object]) -> None:
    written.extend(batch)


@periodic(0.01)
def refresh() -> None:
    sink.submit("tick")


manager = ShutdownManager()
manager.register(refresh, name="refresh", priority=0)  # stop producing first
manager.register(sink, name="sink", priority=10)  # then flush the writer

refresh.start()
time.sleep(0.05)
report = manager.drain(timeout=5)
assert report.ok and not refresh.running
assert written  # queued records were written before closing
```

## The report

`drain(timeout=30, *, strict=False)` returns a `ShutdownReport`: `items` (name, `status` of `"ok"`, `"error"` or `"timeout"`, `error`, `seconds`), plus `ok`, `failed`, `timed_out` and `seconds`. The timeout is one budget for the whole drain; an item that cannot start before it runs out is reported as `"timeout"`. A failing item never stops the others and `drain` does not raise, unless `strict=True`, which raises `ShutdownError` (carrying `.report`).

```python
from python_wrap.lifecycle import ShutdownError, ShutdownManager

manager = ShutdownManager()
manager.register(lambda: 1 / 0, name="flaky")
manager.register(lambda: None, name="fine", priority=1)

try:
    manager.drain(strict=True)
except ShutdownError as error:
    assert [i.name for i in error.report.failed] == ["flaky"]
    assert error.report.items[1].status == "ok"  # later items still ran
```

`manager.shutting_down` turns true as soon as a drain starts; use it in readiness checks so load balancers stop sending traffic. `manager.reset()` clears everything (tests).

## Async

`await manager.adrain(timeout)` does the same on the event loop. Use it for `AsyncPeriodic` and `AsyncBuffered`, which are tied to a loop; a synchronous `drain()` reports them as errors with a hint. Async callables are awaited under the budget; blocking items run in threads.

```python
import asyncio
from python_wrap.lifecycle import ShutdownManager

manager = ShutdownManager()
closed: list[str] = []


async def close_pool() -> None:
    await asyncio.sleep(0.01)
    closed.append("pool")


manager.register(close_pool, name="pool")


async def main() -> None:
    report = await manager.adrain(timeout=5)
    assert report.ok and closed == ["pool"]


asyncio.run(main())
```

## Signals and `atexit`

`install_signal_handlers(signals=(SIGTERM, SIGINT), *, timeout=30, exit=True, chain=True)` (main thread only) drains when a signal arrives. Inside a running event loop it schedules `adrain()`; otherwise it drains right in the handler. Then it calls the previous handler (`chain`) and exits with status `128 + signum` (`exit`). A second signal while draining skips to the exit step, so a second Ctrl-C forces it. `restore_signal_handlers()` puts the old handlers back.

`install_atexit(timeout=30)` drains on a normal interpreter exit; it is safe to call more than once.

<!-- unix-only -->
```python
import os
import signal
import time
from python_wrap.lifecycle import ShutdownManager

manager = ShutdownManager()
manager.register(lambda: print("flushed"), name="flush")
manager.install_signal_handlers([signal.SIGUSR1], exit=False)

os.kill(os.getpid(), signal.SIGUSR1)
time.sleep(0.1)
assert manager.report is not None and manager.report.ok
manager.restore_signal_handlers()
```

After `fork` (gunicorn pre-fork workers, `multiprocessing`) the child starts with an empty registry, because the parent's resources are not the child's to stop. Register in the worker (for example in `post_fork`).

## Framework notes

**FastAPI / Starlette.** Do the work in the lifespan; do not install signal handlers, the server owns them.

<!-- skip -->
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from python_wrap.lifecycle import shutdown


@asynccontextmanager
async def lifespan(app: FastAPI):
    shutdown.register(flush_metrics, name="metrics", priority=10)
    yield
    await shutdown.adrain(timeout=20)


app = FastAPI(lifespan=lifespan)
```

**uvicorn / gunicorn.** Both send `SIGTERM` and wait for the lifespan (uvicorn) or `graceful_timeout` (gunicorn, default 30 s) to finish; keep your drain timeout below it. For gunicorn sync/threaded workers, call `shutdown.drain()` from the `worker_exit` server hook (and register in `post_fork`).

**Kubernetes.** On pod deletion the kubelet runs the `preStop` hook, sends `SIGTERM`, and sends `SIGKILL` after `terminationGracePeriodSeconds` (default 30). Stop new traffic first (a `preStop` of `sleep 5` gives endpoints time to update, and check `shutdown.shutting_down` in the readiness probe), then drain, with a drain timeout comfortably below the grace period.

**Celery.** Drain from the worker's shutdown signal:

<!-- skip -->
```python
from celery.signals import worker_shutdown
from python_wrap.lifecycle import shutdown


@worker_shutdown.connect
def drain_on_shutdown(**_):
    shutdown.drain(timeout=20)
```

## Events

| Event | Fields | When |
| --- | --- | --- |
| `lifecycle.drain_started` | `items` | A drain begins. |
| `lifecycle.item_done` | `status`, `seconds` | One item finished (the event's function is its name). |
| `lifecycle.drain_finished` | `ok`, `seconds` | The drain is complete. |
