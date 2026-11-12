"""The payment gateway client: one function, wrapped in the payments policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orders.fakes import CardDeclined
from python_wrap import Policy


class PaymentDeclined(Exception):
    """The customer's card was refused."""


def make_charge(gateway: Any, policy: Policy) -> Callable[[str, int, str], str]:
    """Return ``charge(order_id, cents, card_token) -> charge id``."""

    @policy
    def charge_via_gateway(order_id: str, cents: int, card_token: str) -> str:
        # The key is derived from the order, so every retry of this call is the same
        # charge as far as the gateway is concerned.
        reply = gateway.charge(
            idempotency_key=f"order-{order_id}", cents=cents, card_token=card_token
        )
        return str(reply["charge_id"])

    def charge(order_id: str, cents: int, card_token: str) -> str:
        try:
            return charge_via_gateway(order_id, cents, card_token)
        except CardDeclined as exc:
            raise PaymentDeclined(str(exc)) from exc

    return charge
