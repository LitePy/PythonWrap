"""The order use case: check stock, charge, reserve, queue the confirmation email."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orders.config import Settings
from orders.inventory import Inventory
from python_wrap import Buffered, count_calls, idempotent, rate_limit

Lines = list[tuple[str, int]]  # (sku, quantity)


def build_create_order(
    settings: Settings,
    inventory: Inventory,
    charge: Callable[[str, int, str], str],
    outbox: Buffered,
) -> Any:
    # Layers, outermost first:
    #   count_calls  - latency and error counters for the whole use case
    #   idempotent   - a client retry with the same request id gets the stored order back
    #   rate_limit   - per user; inside idempotent so a replayed request costs nothing
    @count_calls
    @idempotent(
        key=lambda user_id, request_id, *_, **__: f"{user_id}:{request_id}",
        ttl="1h",
        in_progress="wait",
    )
    @rate_limit(settings.orders_per_minute, 60, key=lambda user_id, *_, **__: user_id)
    def create_order(
        user_id: str, request_id: str, lines: Lines, card_token: str
    ) -> dict[str, Any]:
        total = sum(inventory.require(sku, qty) * qty for sku, qty in lines)
        order_id = f"{user_id}-{request_id}"
        charge_id = charge(order_id, total, card_token)  # raises if the customer cannot pay
        for sku, qty in lines:
            inventory.reserve(sku, qty)
        outbox.submit({"id": order_id, "to": user_id, "subject": f"Order {order_id} confirmed"})
        return {"order_id": order_id, "total_cents": total, "charge_id": charge_id}

    return create_order
