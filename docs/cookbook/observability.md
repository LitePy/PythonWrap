# Observability: redacted logs and an audit trail

## The problem

You need to know *what happened* in production: which function was called, with what, by whom, how long it took and how it ended. You also have to make sure the log never contains a password, a token or a national ID number, because logs are copied to systems with far weaker access control than your database.

Hand-written `logger.info("calling %s with %s", ...)` lines drift out of date, forget to redact, and end up in a different format in every module. The goal here is:

- one decorator per function, so coverage is visible in the code,
- secrets masked **by parameter name**, in one shared list,
- structured (JSON) output your log pipeline can index,
- a separate, structured **audit record** per call for security-relevant operations.

## The code

python-wrap never configures logging. The script below does it explicitly: a JSON formatter on a handler on the application's own logger.

```python
import contextlib
import dataclasses
import io
import json
import logging

from python_wrap import DEFAULT_REDACT, AuditRecord, audit, log_args


class JsonFormatter(logging.Formatter):
    """One JSON object per line; includes the AuditRecord when python-wrap attached one."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        audit_record = getattr(record, "audit", None)
        if isinstance(audit_record, AuditRecord):
            payload["audit"] = dataclasses.asdict(audit_record)
        return json.dumps(payload, default=str)


stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("myapp")
log.addHandler(handler)
log.setLevel(logging.DEBUG)
log.propagate = False  # keep this demo's output out of the root logger

audit_trail: list[AuditRecord] = []  # stand-in for a database table or a log shipper


# `redact=` REPLACES the defaults, so extend DEFAULT_REDACT instead of overwriting it, and
# pass the same list to every decorator: each one masks on its own.
REDACT = (*DEFAULT_REDACT, "ssn")


@log_args(logger=log, redact=REDACT)
@audit(logger=log, sink=audit_trail.append, redact=REDACT)
def change_email(user_id: int, new_email: str, password: str, ssn: str = "") -> bool:
    if "@" not in new_email:
        raise ValueError("invalid email")
    return True


def main() -> None:
    change_email(7, "ana@example.com", password="hunter2", ssn="123-45-6789")
    with contextlib.suppress(ValueError):
        change_email(7, "not-an-email", password="hunter2")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    text = stream.getvalue()
    assert "hunter2" not in text and "123-45-6789" not in text  # secrets never reach the log
    print(lines[0]["message"])

    ok, failed = audit_trail
    assert ok.status == "success" and failed.status == "error"
    assert ok.arguments["password"] == "***"
    print(json.dumps({"function": ok.function, "status": ok.status, "args": ok.arguments}))
    print(json.dumps({"function": failed.function, "status": failed.status, "error": failed.error}))

    audited = [line["audit"] for line in lines if "audit" in line]
    assert [a["status"] for a in audited] == ["success", "error"]


main()
```

## How it works

* **`@log_args`** logs `Calling change_email(user_id=7, new_email='...', password='***', ssn='***')` before the call (default level `DEBUG`). A parameter is masked when its **name contains** any redaction fragment, case-insensitively. Long values are truncated at `max_length` (200).
* **`@audit`** emits one `AuditRecord` per call: `function`, `timestamp` (UTC), `caller` (`"file:line"` of the calling code), `arguments` (masked), `status` (`"success"` or `"error"`), `error` (the exception's `repr`), `duration` and, with `include_result=True`, `result`. It delivers the record to your **`sink`** callable and also attaches it to the log record as `record.audit`, which is how the JSON formatter above finds it.
* **`REDACT = (*DEFAULT_REDACT, "ssn")`** extends the defaults. `DEFAULT_REDACT` covers `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `authorization`, `auth`, `credential`, `private_key`, `cookie` and `session`.
* The sink is where the audit trail goes: a database insert, a queue, a file. An exception raised by the sink is logged and swallowed, so a broken audit store never breaks the audited function. Exceptions from the function itself are recorded (`status="error"`) and re-raised.

Stacking order matters little for correctness (each decorator sees the same arguments), but keep the cheap logging outermost and the audit closest to the function so the recorded `duration` covers only the function.

For latency metrics, `@timing(callback=...)` hands you a `TimingRecord(name, elapsed, success)` for every call, which you can forward to StatsD or Prometheus:

```python
from python_wrap import TimingRecord, timing

samples: list[TimingRecord] = []


@timing(callback=samples.append)  # in production: callback=lambda r: statsd.timing(r.name, r.elapsed)
def lookup(key: str) -> str:
    return key.upper()


lookup("a")
assert samples[0].success and samples[0].name.endswith("lookup")
```

## Pitfalls

!!! warning "Each decorator redacts on its own"
    `redact=` is a per-decorator option and **replaces** the defaults. The demo passes the same `REDACT` tuple to both `log_args` and `audit`; with `redact=("ssn",)` alone, `password` would be logged in clear. Define the list once and reuse it, always built from `DEFAULT_REDACT`.

!!! warning "Redaction is by name, not by value"
    A parameter called `data` that happens to contain a token is logged in full. Redaction does not look inside values, and it does not hide what the function itself logs, returns or puts in an exception message. Name sensitive parameters accordingly (`api_token`, `card_number` plus an entry in `redact`), and keep secrets out of exception messages.

* **`log_args` logs at `DEBUG`** and `audit` at `INFO` by default. Configure levels for the `logger` you pass, or secrets-free argument logs vanish in production when you expected them.
* **`AuditRecord.caller` is a file path and line.** That is what you want for forensics, but treat the log as internal.
* **`include_result=True` stores the return value** in the record. Do not turn it on for functions that return sensitive data; results are not redacted.
* The record is created after the call finishes, so a process that is killed mid-call leaves no audit entry. For operations that must never be missed, write an "intent" row first.
* A sink that blocks (a slow database insert) slows every audited call. Push to a queue in the sink and write asynchronously.

## Test it

Test redaction with a real handler and a string search: the property you care about is "this secret never appears in any log output". Capture the audit trail with a list as the sink. In pytest you would use the `caplog` fixture; the plain handler below works in any test runner.

```python
import contextlib
import dataclasses
import io
import json
import logging

from python_wrap import DEFAULT_REDACT, AuditRecord, audit, log_args


class JsonFormatter(logging.Formatter):
    """One JSON object per line; includes the AuditRecord when python-wrap attached one."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        audit_record = getattr(record, "audit", None)
        if isinstance(audit_record, AuditRecord):
            payload["audit"] = dataclasses.asdict(audit_record)
        return json.dumps(payload, default=str)


stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("myapp")
log.addHandler(handler)
log.setLevel(logging.DEBUG)
log.propagate = False  # keep this demo's output out of the root logger

audit_trail: list[AuditRecord] = []  # stand-in for a database table or a log shipper


# `redact=` REPLACES the defaults, so extend DEFAULT_REDACT instead of overwriting it, and
# pass the same list to every decorator: each one masks on its own.
REDACT = (*DEFAULT_REDACT, "ssn")


@log_args(logger=log, redact=REDACT)
@audit(logger=log, sink=audit_trail.append, redact=REDACT)
def change_email(user_id: int, new_email: str, password: str, ssn: str = "") -> bool:
    if "@" not in new_email:
        raise ValueError("invalid email")
    return True

import pytest

from python_wrap.testing import fake_clock, spy


def reset() -> None:
    stream.seek(0)
    stream.truncate()
    audit_trail.clear()


def test_secrets_never_reach_the_log_output() -> None:
    reset()
    change_email(7, "ana@example.com", password="s3cret-pw", ssn="123-45-6789")
    output = stream.getvalue()
    assert "s3cret-pw" not in output and "123-45-6789" not in output
    assert "password='***'" in output


def test_audit_records_success_and_failure() -> None:
    reset()
    change_email(7, "ana@example.com", password="x")
    with pytest.raises(ValueError):
        change_email(7, "nope", password="x")
    ok, failed = audit_trail
    assert (ok.status, failed.status) == ("success", "error")
    assert "ValueError" in (failed.error or "")
    assert ok.arguments["user_id"] == 7 and ok.arguments["password"] == "***"


def test_json_lines_are_machine_readable() -> None:
    reset()
    change_email(1, "ana@example.com", password="x")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    audit_line = next(line for line in lines if "audit" in line)
    assert audit_line["audit"]["status"] == "success"
    assert audit_line["level"] == "INFO"


def test_a_broken_sink_does_not_break_the_function() -> None:
    reset()

    def broken_sink(record: AuditRecord) -> None:
        raise OSError("audit database is down")

    @audit(sink=broken_sink, logger=log)
    def delete_account(user_id: int) -> str:
        return "deleted"

    assert delete_account(5) == "deleted"  # the business operation still succeeds


def test_durations_are_deterministic_under_a_fake_clock() -> None:
    reset()
    with fake_clock():
        change_email(2, "ana@example.com", password="x")
    assert audit_trail[0].duration == 0.0


def test_spy_confirms_the_sink_is_called_once_per_call() -> None:
    sink = spy(lambda record: None)

    @audit(sink=sink)
    def ping() -> None: ...

    ping()
    ping()
    assert sink.call_count == 2


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```
