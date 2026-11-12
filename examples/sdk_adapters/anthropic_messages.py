"""python-wrap around the Anthropic Python SDK: messages with a tool loop, and a guarded stream.

Targets ``anthropic>=0.40,<1``: ``client.messages.create(model=, max_tokens=, messages=,
tools=)`` returning ``content`` blocks (``type == "text"`` with ``text``; ``type ==
"tool_use"`` with ``id``, ``name``, ``input``), ``stop_reason`` and
``usage.{input_tokens,output_tokens}``; and ``with client.messages.stream(...) as stream``
exposing ``stream.text_stream`` and ``stream.get_final_message()``. python-wrap never imports
the SDK: usage and errors (``status_code``, including 529 overloaded) are read by shape.

Run it:

* ``ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=<model id> \
  python examples/sdk_adapters/anthropic_messages.py``
* ``python examples/sdk_adapters/anthropic_messages.py --offline`` uses a tiny fake client with
  the same response shape (no SDK, no network). Without a key, or without the SDK, the script
  prints a SKIP notice and exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

from python_wrap import RunGuard, ToolRegistry
from python_wrap.llm import Budget, stream_timeout
from python_wrap.presets import llm_client

# --------------------------------------------------------------------- tools

STOCK = {"widget": 12, "gadget": 0}


def build_tools() -> ToolRegistry:
    tools = ToolRegistry(guard=RunGuard(max_steps=8, max_repeats=3, max_seconds=60))

    @tools.tool(read_only=True)
    def stock_level(sku: str) -> int:
        """How many units of an item are in stock."""
        return STOCK.get(sku, 0)

    return tools


TOOL_SCHEMAS = [
    {
        "name": "stock_level",
        "description": "How many units of an item are in stock.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    }
]

# --------------------------------------------------------------------- the client


def build_create(client: Any, spend: Budget) -> Callable[..., Any]:
    """`client.messages.create` behind transient-only retries, a breaker and a token budget."""
    policy = llm_client("anthropic-messages", spend=spend, attempts=3)

    @policy
    def create(**kwargs: Any) -> Any:
        return client.messages.create(**kwargs)

    return create


def run_tool_loop(client: Any, model: str, question: str, spend: Budget) -> str:
    create = build_create(client, spend)
    tools = build_tools()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    with tools.run():
        while True:
            reply = create(model=model, max_tokens=512, messages=messages, tools=TOOL_SCHEMAS)
            if reply.stop_reason != "tool_use":
                return "".join(b.text for b in reply.content if b.type == "text")
            messages.append({"role": "assistant", "content": reply.content})
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(tools.call(block.name, block.input)),
                }
                for block in reply.content
                if block.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})


def stream_answer(client: Any, model: str, question: str, spend: Budget) -> Iterator[str]:
    """Yield text as it arrives; a stalled stream raises `StreamTimeout` instead of hanging.

    The budget cannot see inside a stream, so the final message's usage is recorded by hand.
    """
    with client.messages.stream(
        model=model, max_tokens=512, messages=[{"role": "user", "content": question}]
    ) as stream:
        yield from stream_timeout(iter(stream.text_stream), first_token=20, chunk_gap=10)
        spend.record(stream.get_final_message())


# --------------------------------------------------------------------- offline fake


class FakeOverloaded(Exception):
    """What the SDK raises when the API is overloaded (HTTP 529)."""

    def __init__(self) -> None:
        super().__init__("overloaded")
        self.status_code = 529
        self.response = SimpleNamespace(status_code=529, headers={"retry-after": "1"})


def _message(blocks: list[Any], stop: str) -> Any:
    usage = SimpleNamespace(input_tokens=60, output_tokens=25)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, model="fake-model")


class _FakeStream:
    def __init__(self, parts: list[str]) -> None:
        self.text_stream = iter(parts)
        self._parts = parts

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return _message([SimpleNamespace(type="text", text="".join(self._parts))], "end_turn")


class FakeAnthropic:
    def __init__(self, fail_first: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self._fail = fail_first
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs: Any) -> Any:
        if self._fail:
            self._fail = False
            raise FakeOverloaded
        self.requests.append(kwargs)
        done = any(
            m["role"] == "user" and isinstance(m["content"], list)  # tool results
            for m in kwargs["messages"]
        )
        if done:
            return _message([SimpleNamespace(type="text", text="We have 12 widgets.")], "end_turn")
        use = SimpleNamespace(
            type="tool_use", id="tu_1", name="stock_level", input={"sku": "widget"}
        )
        return _message([use], "tool_use")

    def _stream(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(["Stock ", "is ", "healthy."])


def run_offline(fail_first: bool = False) -> dict[str, Any]:
    client = FakeAnthropic(fail_first=fail_first)
    spend = Budget(tokens=10_000, name="anthropic-demo")
    answer = run_tool_loop(client, "fake-model", "How many widgets?", spend)
    streamed = "".join(stream_answer(client, "fake-model", "Summarise stock", spend))
    return {"answer": answer, "streamed": streamed, "tokens": spend.spent().tokens}


# --------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--offline" in args:
        print(run_offline())
        return 0
    key, model = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_MODEL")
    if not key or not model:
        print("SKIP: set ANTHROPIC_API_KEY and ANTHROPIC_MODEL to call the real API (or --offline)")
        return 0
    try:
        import anthropic  # imported lazily: only needed for the real call
    except ImportError:
        print("SKIP: the anthropic package is not installed (pip install anthropic)")
        return 0
    client = anthropic.Anthropic()
    spend = Budget(tokens=50_000, name="anthropic-demo")
    print(run_tool_loop(client, model, "How many widgets are in stock?", spend))
    print("".join(stream_answer(client, model, "Summarise stock in one line.", spend)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
