"""Structured, redacted logs and an audit trail with ``log_args`` and ``audit``.

``log_args`` logs every call's arguments with secrets masked; ``audit`` produces a
structured ``AuditRecord`` per call (who called, with what, how it ended). Both feed
ordinary ``logging``, here through a tiny JSON formatter, and ``audit`` can also write to
a sink of your choice. A library never configures logging for you, so this script does it
explicitly with a handler on its own logger.

Run: python examples/audit_logging.py
"""

from __future__ import annotations

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


# --- demo ---


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


if __name__ == "__main__":
    main()
