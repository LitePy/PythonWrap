"""Carry the request id and the remaining deadline across process boundaries.

One header pair, configurable with `set_headers`: ``X-Request-ID`` and
``X-Request-Deadline`` (remaining milliseconds). Inbound web middleware reads them
(`trust_deadline_header=True`), `python_wrap.http` sends them (`propagate=True`) and the
Celery integration copies them into task headers. Trace context is out of scope: leave
it to the OpenTelemetry instrumentations.
"""

from __future__ import annotations

import math
from typing import Any

from python_wrap.deadline import remaining
from python_wrap.logs import current_log_context

__all__ = ["deadline_budget", "outgoing_headers", "set_headers"]

_names = {"request_id": "X-Request-ID", "deadline": "X-Request-Deadline"}


def set_headers(*, request_id: str | None = None, deadline: str | None = None) -> None:
    """Rename the request-id and/or deadline header for this process."""
    if request_id:
        _names["request_id"] = request_id
    if deadline:
        _names["deadline"] = deadline


def header_names() -> tuple[str, str]:
    """The ``(request_id, deadline)`` header names in force."""
    return _names["request_id"], _names["deadline"]


def outgoing_headers() -> dict[str, str]:
    """Headers for an outgoing call: the bound request id and the remaining deadline in ms."""
    headers: dict[str, str] = {}
    request_id = current_log_context().get("request_id")
    if request_id:
        headers[_names["request_id"]] = str(request_id)
    left = remaining()
    if left is not None:
        headers[_names["deadline"]] = str(math.floor(left * 1000))
    return headers


def deadline_budget(header: Any, budget: float | None, *, trust: bool) -> float | None:
    """The budget for an inbound request: the smaller of ``budget`` and the header, if trusted.

    ``header`` holds the remaining milliseconds; a missing, unparseable or negative value
    is ignored, so a client can shorten the budget but never extend it.
    """
    if not trust or header is None:
        return budget
    try:
        value = float(header) / 1000
    except (TypeError, ValueError):
        return budget
    if not math.isfinite(value) or value < 0:
        return budget
    return value if budget is None else min(budget, value)
