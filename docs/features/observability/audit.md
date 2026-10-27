# Audit

Record who called a function, with what, and how it ended.

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

Each call produces an `AuditRecord`, delivered to your `sink`, and also attached to the log line as `LogRecord.audit`.

## When to use it

- You need a durable answer to "who deleted that?" for compliance or support.
- You want structured records (not formatted strings) to store in a database or ship to a log pipeline.
- To see arguments while debugging, [`log_args`](logging.md#log_args-and-log_return) is lighter. For latency, use [Timing](timing.md).

## The record

| Field | Meaning |
| --- | --- |
| `function` | Qualified name of the audited function. |
| `timestamp` | UTC, timezone-aware. |
| `caller` | `"file:line"` of the calling code. |
| `arguments` | Arguments by parameter name, with sensitive ones masked. |
| `status` | `"success"` or `"error"`. |
| `error` | The `repr` of the exception, or `None`. |
| `duration` | Seconds the call took. |
| `result` | The return value, only with `include_result=True`. |
| `context` | Fields bound by [`log_context`](logging.md#log_context) when the call started (an empty dict when none). |

To read the record from a log handler instead of a sink, use `record.audit` on the `LogRecord`. `audit` logs at `INFO` by default and takes the same `logger=` and `level=` options as the [logging decorators](logging.md#configuring-logging).

## Redaction

`audit` masks parameters by name, exactly like `log_args`. Add names with `redact_extra=`; `redact=` replaces the defaults. See [Redaction](logging.md#redaction) for the default list and the warning about `redact=`.

```python
from python_wrap import audit

records = []

@audit(redact_extra=["ssn"], sink=records.append)
def register(name: str, ssn: str, password: str) -> None: ...

register("ana", "123-45-6789", "hunter2")
args = records[0].arguments
assert args["ssn"] == "***" and args["password"] == "***" and args["name"] == "ana"
```

## Gotchas

!!! note "A broken sink never breaks the function"
    An exception raised by the sink is logged and swallowed, so a broken audit store never breaks the audited function. Exceptions from the function itself are recorded and re-raised.

!!! warning "Redaction is by name"
    Values are not scanned. A secret passed in a parameter called `data` is recorded as is; add the name with `redact_extra=` or rename the parameter.

## See also

- [Logging](logging.md), including [`log_context`](logging.md#log_context)
- [Project-wide configuration](../configuration.md) for setting redaction names once
- [API reference](../../api/logs.md)
