"""In-memory stand-ins for the outside world, so the project runs offline.

Replace each class with a real client in production; the rest of the code does not change.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class CardDeclined(Exception):
    """The gateway refused the card. A permanent answer, never retried."""


class FakePaymentGateway:
    """Fails with ``ConnectionError`` ``failures`` times (or forever), then charges.

    Like a real gateway it honours the idempotency key: the same key never charges twice.
    """

    def __init__(self, failures: int = 0, *, down: bool = False) -> None:
        self.failures = failures
        self.down = down
        self.attempts = 0
        self.charges: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def charge(self, *, idempotency_key: str, cents: int, card_token: str) -> dict[str, Any]:
        with self._lock:
            self.attempts += 1
            if self.down or self.failures > 0:
                self.failures = max(0, self.failures - 1)
                raise ConnectionError("gateway unreachable")
            if card_token == "tok_declined":
                raise CardDeclined("card declined")
            return self.charges.setdefault(
                idempotency_key,
                {"charge_id": f"ch_{len(self.charges) + 1}", "cents": cents},
            )

    def ping(self) -> bool:
        return not self.down


class FakeInventoryDb:
    """A slow product table: every ``lookup`` is a (counted) query."""

    def __init__(self, products: dict[str, tuple[int, int]]) -> None:
        self.products = dict(products)  # sku -> (stock, price_cents)
        self.queries: list[str] = []
        self._lock = threading.Lock()

    def lookup(self, sku: str) -> dict[str, int]:
        with self._lock:
            self.queries.append(sku)
            stock, price = self.products[sku]
        time.sleep(0.02)  # a slow query
        return {"stock": stock, "price_cents": price}

    def decrement(self, sku: str, quantity: int) -> None:
        with self._lock:
            stock, price = self.products[sku]
            self.products[sku] = (stock - quantity, price)


class FakeMailer:
    """Collects mail. Delivery is at least once, so it de-duplicates by message id."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.sent: dict[str, dict[str, Any]] = {}

    def send_batch(self, rows: list[dict[str, Any]]) -> None:
        if self.down:
            raise ConnectionError("smtp relay down")
        for row in rows:
            self.sent[row["id"]] = row
