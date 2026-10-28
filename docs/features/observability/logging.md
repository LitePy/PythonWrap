# Logging

Log what a function was called with, what it returned, and who called it.

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

The examples on these pages attach a small handler that collects log records, so they can show exactly what is logged. In your own code you configure logging once, as described below.

## When to use it

- You want to see arguments and results while developing or debugging.
- You want request ids or job names on every log line (`log_context`).
- You need to know which code path called a function (`trace`).
- For a structured record of who did what, use [Audit](audit.md). For durations, use [Timing](timing.md).

## Configuring logging

python-wrap **never configures logging**. Importing it does not touch the root logger, and the `python_wrap` logger only carries a `NullHandler`. Until your application sets logging up, the decorators on these pages are silent.

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

## `log_args` and `log_return`

`log_args` logs each call's arguments by parameter name, before running it. `log_return` logs the return value, or the exception type if it raises. See the example at the top of the page.

Both truncate long values (`max_length`, default 200), and both default to `DEBUG` (in v0.1, `log_args` logged at `INFO`).

## Redaction

`log_args` (and [`audit`](audit.md)) mask parameters whose **name contains** any of the redaction fragments, case-insensitively. The masked value is shown as `'***'`.

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

To **add** names without replacing the defaults, use `redact_extra=`, on `log_args` and on `audit`:

```python
from python_wrap import audit

records = []

@audit(redact_extra=["ssn"], sink=records.append)
def register(name: str, ssn: str, password: str) -> None: ...

register("ana", "123-45-6789", "hunter2")
args = records[0].arguments
assert args["ssn"] == "***" and args["password"] == "***" and args["name"] == "ana"
```

To apply the same names to every `log_args` and `audit` in the project, set them once with `configure(redact_extra=[...])` (or replace the whole list with `configure(redact=[...])`); see [Project-wide configuration](../configuration.md).

!!! warning "`redact=` replaces the defaults"
    Passing `redact=("ssn",)` masks only `ssn`; `password` is no longer masked. Prefer `redact_extra=`. Redaction works by parameter **name**; it does not scan values, and it does not touch what the function itself logs or returns.

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

## `log_context`

Bind fields such as a request id once, and have every python-wrap log line made underneath carry them. It works as a context manager (`with` and `async with`) and as a decorator, and blocks nest.

```python
import logging
from python_wrap import log_args
from python_wrap.logs import current_log_context, log_context

class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

log = logging.getLogger("context-demo")
log.setLevel(logging.DEBUG)
handler = ListHandler()
log.addHandler(handler)

@log_args(logger=log)
def charge(amount: int) -> None: ...

with log_context(request_id="r-42"):
    with log_context(user="ana"):
        assert current_log_context() == {"request_id": "r-42", "user": "ana"}
        charge(5)

record = handler.records[0]
assert record.getMessage() == "Calling charge(amount=5) [request_id=r-42 user=ana]"
assert record.context == {"request_id": "r-42", "user": "ana"}   # type: ignore[attr-defined]
assert current_log_context() == {}
```

`log_args`, `log_return`, `audit`, `trace` and `timing` append ` [key=value ...]` to their message and attach the dict as `LogRecord.context`, so a JSON formatter can emit the fields as proper keys. `AuditRecord.context` holds the fields bound when the call started (an empty dict when none).

As a decorator, the fields are bound for each call:

```python
import asyncio
from python_wrap.logs import current_log_context, log_context

@log_context(job="nightly")
async def run() -> dict[str, object]:
    return current_log_context()

assert asyncio.run(run()) == {"job": "nightly"}
```

!!! note "New threads and the context"
    The fields live in a `contextvars.ContextVar`: every asyncio task and thread sees its own. On a regular CPython build a new thread starts **empty**; to carry the fields over, start it in a copied context with `contextvars.copy_context().run(...)` (`run_in_thread` and `asyncio.to_thread` already do this). Free-threaded builds (3.14t) start threads in a copy of the caller's context by default (`sys.flags.thread_inherit_context`), so the fields carry over on their own.

## See also

- [Audit](audit.md), [Timing](timing.md), [Metrics and events](metrics.md)
- [Project-wide configuration](../configuration.md)
- [API reference](../../api/logs.md)
