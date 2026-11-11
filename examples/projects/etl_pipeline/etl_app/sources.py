"""Fake source APIs standing in for real HTTP clients (no network)."""

from __future__ import annotations

import time
from typing import Any

from python_wrap.chaos import fault_point


class SourceAPI:
    """One upstream system: a list of rows, a status endpoint, optional per-row latency."""

    def __init__(self, name: str, rows: int, latency: float = 0.0) -> None:
        self.name = name
        self.latency = latency
        self._rows = [
            {"id": f"{name}-{n}", "amount": 100 + n, "currency": "EUR"} for n in range(rows)
        ]
        self.fetches = 0
        self.progress_pings: list[int] = []

    @fault_point("etl.fetch")
    def fetch_rows(self) -> list[dict[str, Any]]:
        self.fetches += 1
        return list(self._rows)

    def row_work(self) -> None:
        """The per-row latency of talking to this system."""
        if self.latency:
            time.sleep(self.latency)

    def ping_progress(self, done: int) -> None:
        """A rate-limited status endpoint: callers should not hammer it."""
        self.progress_pings.append(done)
