# Observability

Find out how long calls take, how much they allocate, what they were called with, and who called them.

## Configuring logging

python-wrap **never configures logging**. Importing it does not touch the root logger, and the `python_wrap` logger only carries a `NullHandler`. Until your application sets logging up, the decorators below are silent.

Every logging decorator takes two options:

- `logger=` a `logging.Logger`, a logger name, or `None` for the package logger `python_wrap`.
- `level=` the log level (`timing` and `audit` default to `INFO`; `log_args`, `log_return` and `trace` default to `DEBUG`).

Turn everything on for development:

<!-- skip -->
```python
import logging

logging.basicConfig(level=logging.DEBUG)
```

Or route and filter like any other library:

<!-- skip -->
```python
import logging

# Only python-wrap's messages, to their own handler, at their own level.
logger = logging.getLogger("python_wrap")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

# Or silence it completely.
logging.getLogger("python_wrap").setLevel(logging.CRITICAL + 1)
```

To send one decorator's output somewhere specific, name the logger:

```python
import logging
from python_wrap import timing

@timing(logger="myapp.perf", level=logging.DEBUG)
def work() -> None: ...

work()
```

The examples below attach a small handler that collects log records, so they can show exactly what is logged. In your own code you configure logging once, as above.

## `timing`

Measure and log how long each call takes. Async functions are timed across the whole awaited coroutine. A call that raises is logged as `<name> raised ValueError after 12.34 ms` and the exception propagates.

=== "Sync"

    ```python
    import logging
    from python_wrap import TimingRecord, timing

    records: list[TimingRecord] = []

    @timing(logger="demo", threshold=0.5, callback=records.append)
    def fast() -> int:
        return 1

    fast()
    assert records[0].success is True
    assert records[0].name.endswith("fast")
    assert records[0].elapsed < 0.5
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimingRecord, timing

    records: list[TimingRecord] = []

    @timing(callback=records.append)
    async def pause() -> None:
        await asyncio.sleep(0.02)

    asyncio.run(pause())
    assert records[0].elapsed >= 0.015   # the awaited time is measured
    ```

| Option | Meaning |
| --- | --- |
| `threshold` | Only log successful calls slower than this many seconds. Failed calls are always logged. |
| `callback` | Called with a `TimingRecord(name, elapsed, success)` for **every** call, regardless of `threshold`. Use it to feed a metrics system. Errors in the callback are logged and swallowed. |

`timing` uses `time.perf_counter`.

## `benchmark`

Run a function repeatedly and get statistics. It is a **plain function, not a decorator** (see [Migration](../migration.md#benchmark-is-a-function)).

=== "Sync"

    ```python
    from python_wrap import benchmark

    result = benchmark(sorted, args=([3, 1, 2],), runs=10, warmup=2)
    assert result.runs == 10
    assert result.min <= result.median <= result.max
    print(result)   # sorted: 10 runs, mean 1.2 us, median 1.1 us, ...
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import benchmark_async

    async def work(n: int) -> int:
        await asyncio.sleep(0)
        return n

    async def main() -> None:
        result = await benchmark_async(work, args=(1,), runs=3)
        assert result.runs == 3

    asyncio.run(main())
    ```

`BenchmarkResult` has `name`, `runs`, `timings`, `mean`, `median`, `min`, `max` and `stdev`, all in seconds. `benchmark` raises `TypeError` for an `async def` function (use `benchmark_async`).

## `measure_memory`

Measure the Python memory a call allocates, using `tracemalloc`. Works on every OS and logs `<name> peak memory 1.00 MiB (net +12.00 KiB)`.

```python
from python_wrap import MemoryRecord, measure_memory

records: list[MemoryRecord] = []

@measure_memory(callback=records.append)
def build() -> list[int]:
    return [0] * 100_000

data = build()
assert records[0].peak_bytes >= 100_000 * 8 * 0.5   # at least roughly the list's size
assert records[0].net_bytes > 0                     # the list is still alive
```

`peak_bytes` is the highest traced allocation during the call, relative to its start; `net_bytes` is the memory still held at the end minus at the start (it can be negative).

!!! note
    `tracemalloc` is process-global. Allocations by other threads or tasks while the call runs are counted too, and tracing slows the program. See [Limitations](../limitations.md#measure_memory-and-profile).

## `profile`

Profile each call with `cProfile` and log the top rows.

```python
import pstats
from python_wrap import profile

captured: list[pstats.Stats] = []

@profile(sort="tottime", limit=5, callback=captured.append)
def work() -> int:
    return sum(i * i for i in range(1000))

work()
assert isinstance(captured[0], pstats.Stats)
```

`sort` is any `pstats.SortKey` value (`"cumulative"` by default, or `"tottime"`, `"calls"`, ...); `limit` is the number of rows logged (`None` for all). If another profiler is already active, a warning is logged once and the function runs unprofiled.

## `log_args` and `log_return`

`log_args` logs each call's arguments by parameter name, before running it. `log_return` logs the return value, or the exception type if it raises.

```python
import logging
from python_wrap import log_args, log_return

class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

log = logging.getLogger("demo")
log.setLevel(logging.DEBUG)
handler = ListHandler()
log.addHandler(handler)

@log_args(logger=log)
@log_return(logger=log)
def add(a: int, b: int) -> int:
    return a + b

add(1, b=2)
assert "Calling add(a=1, b=2)" in handler.messages
assert "add returned 3" in handler.messages
```

Both truncate long values (`max_length`, default 200), and both default to `DEBUG` (in v0.1, `log_args` logged at `INFO`).

### Redaction

`log_args` (and `audit`) mask parameters whose **name contains** any of the redaction fragments, case-insensitively. The masked value is shown as `'***'`.

The default list is `DEFAULT_REDACT`: `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `authorization`, `auth`, `credential`, `private_key`, `cookie`, `session`.

```python
import logging
from python_wrap import DEFAULT_REDACT, log_args

class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

log = logging.getLogger("redaction-demo")
log.setLevel(logging.DEBUG)
sink = ListHandler()
log.addHandler(sink)

@log_args(logger=log)
def login(user: str, password: str) -> None: ...

login("ana", "hunter2")
assert "hunter2" not in sink.messages[0]
assert "password='***'" in sink.messages[0]

# Extend the defaults, or pass () to disable redaction.
@log_args(logger=log, redact=(*DEFAULT_REDACT, "ssn"))
def register(name: str, ssn: str) -> None: ...

register("ana", "123-45-6789")
assert "123-45-6789" not in sink.messages[1]
```

!!! warning "`redact=` replaces the defaults"
    Passing `redact=("ssn",)` masks only `ssn`; `password` is no longer masked. Extend `DEFAULT_REDACT` as shown above. Redaction works by parameter **name**; it does not scan values, and it does not touch what the function itself logs or returns.

## `audit`

Record who called a function, with what, and how it ended. Each call produces an `AuditRecord`, delivered to your `sink`, and also attached to the log line as `LogRecord.audit`.

```python
from python_wrap import AuditRecord, audit

records: list[AuditRecord] = []

@audit(sink=records.append, include_result=True)
def delete_user(user_id: int, token: str) -> str:
    if user_id < 0:
        raise ValueError("bad id")
    return "deleted"

delete_user(7, token="abc123")
try:
    delete_user(-1, token="abc123")
except ValueError:
    pass

ok, failed = records
assert ok.status == "success" and ok.result == "deleted"
assert ok.arguments == {"user_id": 7, "token": "***"}   # sensitive values are masked
assert failed.status == "error" and "ValueError" in failed.error
assert ok.caller.endswith(tuple("0123456789"))          # "file:line" of the calling code
```

`AuditRecord` fields: `function`, `timestamp` (UTC, timezone-aware), `caller` (`"file:line"`), `arguments`, `status` (`"success"` or `"error"`), `error` (the `repr` of the exception, or `None`), `duration` and `result` (only with `include_result=True`).

An exception raised by the sink is logged and swallowed, so a broken audit store never breaks the audited function. Exceptions from the function itself are recorded and re-raised.

To read the record from a log handler instead, use `record.audit` on the `LogRecord`.

## `trace`

Log entry, the call stack that led to the call, and exit. Three messages per call: `Entering f`, the stack (python-wrap's own frames left out, outermost first), and `Exiting f` (or `Exiting f (raised ValueError)`).

```python
from python_wrap import trace

@trace(limit=5)   # keep the 5 innermost frames
def handler() -> int:
    return 1

assert handler() == 1
```

Use it to answer "who is calling this?" while debugging. It defaults to `DEBUG`, so it is silent unless you enable that level.
