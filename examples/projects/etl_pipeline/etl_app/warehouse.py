"""The load target. `make_loader` returns a replay-safe writer: one write per row id."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from python_wrap import idempotent
from python_wrap.idempotency import MemoryResultStore


class Warehouse:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes = 0
        self._lock = threading.Lock()

    def insert(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.writes += 1
            self.rows[row["id"]] = row


def make_loader(warehouse: Warehouse) -> Callable[[dict[str, Any]], str]:
    """Key the write on the business id: replaying a chunk after a crash writes nothing twice."""

    @idempotent(key=lambda row: f"load:{row['id']}", ttl="1d", store=MemoryResultStore())
    def load_row(row: dict[str, Any]) -> str:
        warehouse.insert(row)
        return str(row["id"])

    return load_row
