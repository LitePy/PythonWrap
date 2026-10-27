# Project-wide configuration

Repeating `retry(max_attempts=5, delay=0.5, ...)` or `redact=(...)` on every decorator is how defaults drift. `python_wrap.config` sets them once.

```python
import logging
from python_wrap.config import configure, get_config, reset_config

configure(
    logger=logging.getLogger("myapp"),
    retry={"max_attempts": 5, "delay": 0.5, "backoff": 2.0, "max_time": 30},
    redact_extra=["ssn", "iban"],
)
assert get_config()["retry"]["max_attempts"] == 5
reset_config()
```

## When values are read

Call `configure` **once, at start-up, before you decorate anything**. Defaults are read when a decorator is *applied* (at import time for module-level functions), not on every call. A decorator applied earlier keeps the settings it saw. An explicit argument always beats a configured default.

```python
from python_wrap import retry
from python_wrap.config import configure, reset_config

@retry
def early() -> None: ...      # 3 attempts: applied before configure()

configure(retry={"max_attempts": 7})

@retry
def late() -> None: ...       # 7 attempts

@retry(max_attempts=2)
def explicit() -> None: ...   # 2 attempts: explicit wins

reset_config()
```

## Keys

| Key | Effect |
| --- | --- |
| `logger` | Logger (or name) used by every decorator that logs and was not given `logger=`. `None` restores the package logger. |
| `retry` | Dict of defaults for `retry` and `retrying`: `max_attempts`, `delay`, `backoff`, `max_delay`, `jitter`, `max_time`, `log_level`. |
| `redact` | Replaces the built-in `DEFAULT_REDACT` list for `log_args` and `audit`. `None` restores it. |
| `redact_extra` | Names **added** to the redaction list (the built-ins stay), for `log_args` and `audit`. |

Each key you pass replaces that key as a whole (`retry={...}` replaces the previous retry defaults); keys you don't mention are untouched. Unknown keys raise `TypeError` with a hint:

```python
from python_wrap.config import configure

try:
    configure(retyr={"max_attempts": 3})
except TypeError as exc:
    assert "Did you mean 'retry'" in str(exc)
```

Retry defaults are validated immediately (`ValueError` for `max_attempts=0`, and so on). Because `redact=` on a decorator replaces the list, prefer `redact_extra` in project settings; it is also what `redact_extra=` does on `log_args` and `audit` themselves.

## In tests

`configured()` applies defaults for a block and restores the previous state on exit, even after an error. Apply the decorators inside the block.

```python
from python_wrap import retry
from python_wrap.config import configured, get_config

with configured(retry={"max_attempts": 1}):
    @retry
    def once() -> None: ...

assert get_config()["retry"] == {}
```

`reset_config()` forgets everything; `get_config()` returns a copy of what is in force.
