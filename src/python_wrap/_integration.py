"""Shared request/task context plumbing for optional framework adapters."""

from __future__ import annotations

import contextlib
import re
import uuid
from typing import Any

from python_wrap._runtime import seconds
from python_wrap.deadline import deadline
from python_wrap.logs import log_context


def request_id(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", text) else uuid.uuid4().hex


def scope(fields: dict[str, Any], budget: float | None = None) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(log_context(**fields))
        if budget is not None:
            stack.enter_context(deadline(seconds(budget, positive=False)))
    except BaseException:
        stack.close()
        raise
    return stack
