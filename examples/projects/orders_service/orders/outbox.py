"""Email outbox: orders enqueue a message and return; a background thread delivers it.

``journal=SQLiteJournal(...)`` writes each message to disk before ``submit`` returns, so a
crash or a mail-server outage loses nothing: a restarted outbox calls ``replay()``.
Delivery is at least once, so the mailer de-duplicates by message id.
"""

from __future__ import annotations

import logging
from typing import Any

from python_wrap import Buffered, buffered
from python_wrap.buffering import SQLiteJournal

_log = logging.getLogger("orders.outbox")
_log.addHandler(logging.NullHandler())


def make_outbox(path: str, mailer: Any, flush_interval: float) -> Buffered:
    @buffered(
        max_size=20,
        flush_interval=flush_interval,
        journal=SQLiteJournal(path),
        logger=_log,
    )
    def deliver(batch: list[dict[str, Any]]) -> None:
        mailer.send_batch(batch)

    assert isinstance(deliver, Buffered)
    return deliver


def close_outbox(outbox: Buffered) -> None:
    outbox.close(timeout=2)
    if outbox.journal is not None:
        outbox.journal.close()
