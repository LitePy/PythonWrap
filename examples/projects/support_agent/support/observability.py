"""What gets written to the audit tape, and with what removed."""

from __future__ import annotations

from python_wrap import AuditTape, Redactor


def make_redactor() -> Redactor:
    # Emails, card numbers and secrets are masked by default. Add what is specific to you:
    # here, internal ticket references.
    return Redactor(patterns=[r"TKT-\d{4,}"], max_chars=500)


def make_tape(path: str, redactor: Redactor) -> AuditTape:
    """A JSONL tape: one redacted line per LLM or tool call, readable after a crash."""
    return AuditTape(path, redactor=redactor)
