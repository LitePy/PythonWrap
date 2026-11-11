"""python-wrap around the OpenAI Python SDK: a chat call with retries, a budget and a tool loop.

Targets ``openai>=1.40,<3``: ``client.chat.completions.create(model=, messages=, tools=)``
returning ``choices[0].message`` with ``content`` / ``tool_calls[i].function.{name,arguments}``
and ``usage.{prompt_tokens,completion_tokens}``; and ``client.responses.create(model=,
input=)`` with ``output_text`` and ``usage.{input_tokens,output_tokens}``. python-wrap never
imports the SDK: it reads usage and errors (``status_code``, ``response.headers``) by shape.

Run it:

* ``OPENAI_API_KEY=... OPENAI_MODEL=<model id> python examples/sdk_adapters/openai_chat.py``
* ``python examples/sdk_adapters/openai_chat.py --offline`` uses a tiny fake client with the
  same response shape (no SDK, no network). Without a key, or without the SDK, the script
  prints a SKIP notice and exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from python_wrap import RunGuard, ToolRegistry, retry
from python_wrap.llm import Budget
from python_wrap.presets import llm_client

# --------------------------------------------------------------------- tools

ORDERS = {"A-1": "shipped", "B-2": "processing"}
REFUNDS: list[str] = []


def build_tools() -> ToolRegistry:
    """Tools the model may call. The write tool is replay-safe; the read tool is retried."""
    tools = ToolRegistry(guard=RunGuard(max_steps=8, max_repeats=3, max_seconds=60))

    @tools.tool(read_only=True, retry=retry(max_attempts=3, delay=0.1, on=ConnectionError))
    def order_status(order_id: str) -> str:
        """Look up the status of an order."""
        return ORDERS.get(order_id, "unknown")

    @tools.tool(idempotent_key=lambda order_id: order_id)  # one refund per order, ever
    def refund(order_id: str) -> str:
        """Refund an order."""
        REFUNDS.append(order_id)
        return f"refunded {order_id}"

    return tools


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "order_status",
            "description": "Look up the status of an order.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]

# --------------------------------------------------------------------- the client


def build_chat(client: Any, spend: Budget) -> Callable[..., Any]:
    """`client.chat.completions.create` behind retries (429/5xx only), a breaker and a budget."""
    policy = llm_client("openai-chat", spend=spend, attempts=3)

    @policy
    def chat(**kwargs: Any) -> Any:
        return client.chat.completions.create(**kwargs)

    return chat


def run_tool_loop(client: Any, model: str, question: str, spend: Budget) -> str:
    chat = build_chat(client, spend)
    tools = build_tools()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    with tools.run():  # step, repeat and time limits for the whole conversation
        while True:
            message = chat(model=model, messages=messages, tools=TOOL_SCHEMAS).choices[0].message
            if not message.tool_calls:
                return str(message.content)
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            for call in message.tool_calls:
                result = tools.call(call.function.name, call.function.arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )


def ask_once(client: Any, model: str, question: str) -> str:
    """The Responses API gets the same policy: `llm_client` does not care about the endpoint."""
    call = llm_client("openai-responses", attempts=3)(client.responses.create)
    return str(call(model=model, input=question).output_text)


# --------------------------------------------------------------------- offline fake


class FakeRateLimit(Exception):
    """What the SDK raises on HTTP 429: ``status_code`` plus a ``response`` with headers."""

    def __init__(self) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.response = SimpleNamespace(status_code=429, headers={"retry-after": "1"})


def _usage(prompt: int, completion: int) -> Any:
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def _reply(content: str | None, calls: list[Any] | None = None) -> Any:
    message = SimpleNamespace(role="assistant", content=content, tool_calls=calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=_usage(50, 20),
        model="fake-model",
    )


class FakeOpenAI:
    """Same surface as ``OpenAI()`` for the calls above; replies follow a script."""

    def __init__(self, fail_first: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self._fail = fail_first
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)
        self.responses = SimpleNamespace(create=self._respond)

    def _create(self, **kwargs: Any) -> Any:
        if self._fail:
            self._fail = False
            raise FakeRateLimit
        self.requests.append(kwargs)
        if not any(m["role"] == "tool" for m in kwargs["messages"]):
            call = SimpleNamespace(
                id="call_1",
                type="function",
                function=SimpleNamespace(name="order_status", arguments='{"order_id": "A-1"}'),
            )
            return _reply(None, [call])
        return _reply("Order A-1 has shipped.")

    def _respond(self, **kwargs: Any) -> Any:
        return SimpleNamespace(
            output_text="pong", usage=SimpleNamespace(input_tokens=5, output_tokens=1)
        )


def run_offline(fail_first: bool = False) -> dict[str, Any]:
    client = FakeOpenAI(fail_first=fail_first)
    spend = Budget(tokens=10_000, name="openai-demo")
    answer = run_tool_loop(client, "fake-model", "Where is order A-1?", spend)
    return {
        "answer": answer,
        "tokens": spend.spent().tokens,
        "requests": len(client.requests),
        "pong": ask_once(client, "fake-model", "ping"),
    }


# --------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--offline" in args:
        print(run_offline())
        return 0
    key, model = os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENAI_MODEL")
    if not key or not model:
        print("SKIP: set OPENAI_API_KEY and OPENAI_MODEL to call the real API (or use --offline)")
        return 0
    try:
        import openai  # imported lazily: only needed for the real call
    except ImportError:
        print("SKIP: the openai package is not installed (pip install openai)")
        return 0
    client = openai.OpenAI()
    spend = Budget(tokens=50_000, name="openai-demo")
    print(run_tool_loop(client, model, "Where is order A-1?", spend))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
