"""A scripted fake model, so the agent runs offline and deterministically.

``scripted_support_model`` decides its next move from the conversation so far, like a real
model would: look the order up, refund it, tell the customer, then answer. Because it
reads the conversation instead of counting calls, a primary and a backup provider that
share it behave identically, whichever one serves a given turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from python_wrap import FakeResponse, fake_llm
from python_wrap.llm_testing import FakeCall, FakeProvider, ToolCall, make_usage

TOKENS_PER_CALL = (120, 30)  # input, output


def _reply(*calls: ToolCall, text: str = "") -> FakeResponse:
    return FakeResponse(
        text=text,
        tool_calls=calls,
        usage=make_usage("anthropic", *TOKENS_PER_CALL),
        stop_reason="tool_use" if calls else "end_turn",
    )


def scripted_support_model(refund_cents: int | None = None) -> Callable[[FakeCall], FakeResponse]:
    """A model that handles "my order A-1234 arrived broken" tickets.

    Args:
        refund_cents: How much to refund; None refunds the whole order total.
    """

    def respond(call: FakeCall) -> FakeResponse:
        messages: list[dict[str, Any]] = call.messages
        ticket = messages[0]["content"]
        order_id = re.search(r"[A-Z]-\d+", ticket)
        assert order_id, "the ticket must mention an order id"
        results = {m["name"]: m["content"] for m in messages if m["role"] == "tool"}

        if "lookup_order" not in results:
            args = json.dumps({"order_id": order_id.group()})
            return _reply(ToolCall("lookup_order", args, id="c1"))
        order = json.loads(results["lookup_order"])
        if "refund" not in results:
            cents = refund_cents if refund_cents is not None else order["total_cents"]
            args = json.dumps({"order_id": order_id.group(), "cents": cents, "reason": "damaged"})
            return _reply(ToolCall("refund", args, id="c2"))
        if "send_email" not in results:
            args = json.dumps(
                {
                    "to": order["customer"],
                    "subject": "About your order",
                    "body": f"Update on your damaged order: {results['refund'][:80]}",
                }
            )
            return _reply(ToolCall("send_email", args, id="c3"))
        return _reply(text=f"Handled {order_id.group()}: refund processed and customer emailed.")

    return respond


def make_provider(name: str, refund_cents: int | None = None) -> FakeProvider:
    """A fake model API. Its fault point is ``llm.<name>``, which is what chaos rules target."""
    return fake_llm(default=scripted_support_model(refund_cents), name=name, model=name)
