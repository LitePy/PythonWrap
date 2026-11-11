"""Fake business systems the tools talk to. Swap them for real clients in production."""

from __future__ import annotations

from typing import Any

ORDERS: dict[str, dict[str, Any]] = {
    "A-1001": {"customer": "pat@example.com", "total_cents": 4200, "status": "delivered"},
    "A-1002": {"customer": "lee@example.com", "total_cents": 90000, "status": "delivered"},
}


class OrderBook:
    def __init__(self) -> None:
        self.orders = {key: dict(value) for key, value in ORDERS.items()}
        self.lookups = 0

    def get(self, order_id: str) -> dict[str, Any] | None:
        self.lookups += 1
        return self.orders.get(order_id)


class PaymentsApi:
    """Records refunds. A real one would call a payment provider."""

    def __init__(self) -> None:
        self.refunds: list[dict[str, Any]] = []

    def refund(self, order_id: str, cents: int, reason: str) -> str:
        self.refunds.append({"order_id": order_id, "cents": cents, "reason": reason})
        return f"rf_{len(self.refunds)}"


class Mailbox:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
