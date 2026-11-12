"""The agent's tools and the rules that govern them.

* ``lookup_order``  read-only: allowed in read-only and dry runs, never deduplicated.
* ``refund``        money moves: needs approval, and one refund per order, ever.
* ``send_email``    a write: a replayed step must not mail the customer twice.
"""

from __future__ import annotations

from typing import Any

from python_wrap import (
    AuditTape,
    Redactor,
    RunGuard,
    ToolCall,
    ToolPolicy,
    ToolRegistry,
    traced_tool,
)
from python_wrap.llm import Budget
from support.backends import Mailbox, OrderBook, PaymentsApi
from support.config import Settings


def make_approver(limit_cents: int) -> Any:
    """Auto-approve small refunds; refuse the rest (a real one would ask a person)."""

    def approver(call: ToolCall) -> bool:
        cents = call.arguments.get("cents")
        return isinstance(cents, int) and cents <= limit_cents

    return approver


def build_registry(
    settings: Settings,
    *,
    orders: OrderBook,
    payments: PaymentsApi,
    mailbox: Mailbox,
    tape: AuditTape,
    redactor: Redactor,
    budget: Budget,
) -> ToolRegistry:
    registry = ToolRegistry(
        guard=RunGuard(
            max_steps=settings.max_steps,
            max_repeats=settings.max_repeats,
            budget=budget,
            name="support-run",
        ),
        policy=ToolPolicy(approver=make_approver(settings.refund_limit_cents), approval_timeout=5),
    )
    trace = {"tape": tape, "redactor": redactor}

    @registry.tool(read_only=True)
    @traced_tool("lookup_order", **trace)
    def lookup_order(order_id: str) -> dict[str, Any]:
        """Look up an order by id."""
        return orders.get(order_id) or {"error": f"no order {order_id}"}

    @registry.tool(
        requires_approval=True,
        idempotent_key=lambda order_id, *_, **__: f"refund-{order_id}",
    )
    @traced_tool("refund", **trace)
    def refund(order_id: str, cents: int, reason: str) -> dict[str, Any]:
        """Refund part or all of an order."""
        return {"refund_id": payments.refund(order_id, cents, reason), "cents": cents}

    @registry.tool
    @traced_tool("send_email", **trace)
    def send_email(to: str, subject: str, body: str) -> str:
        """Email the customer."""
        mailbox.send(to, subject, body)
        return "sent"

    return registry
