"""Audit trail and replay for an LLM tool loop, with a fake provider (no network).

A tiny "agent" asks a fake model for an answer, calls a tool, and retries a flaky tool.
`traced_llm` / `traced_tool` record every call to an `AuditTape` with secrets, emails and
card numbers redacted; `replay` then serves the recorded results so the same flow runs in a
test without calling anything, and fails loudly when the flow changes.
"""

from __future__ import annotations

from typing import Any

from python_wrap import (
    AuditTape,
    ReplayMismatch,
    genai,
    replay,
    retry,
    traced_llm,
    traced_tool,
)
from python_wrap.events import listening
from python_wrap.testing import fake_clock

PRICES = {"demo-model": (3.0, 15.0)}  # USD per million input / output tokens


def build(tape: AuditTape) -> tuple[Any, Any]:
    flaky = {"left": 1}

    @traced_llm(model="demo-model-1", system="demo", prices=PRICES, tape=tape)
    def ask(prompt: str) -> dict[str, Any]:
        return {"text": f"echo: {prompt}", "usage": {"input_tokens": 120, "output_tokens": 40}}

    @traced_tool("lookup_customer", tape=tape)
    @retry(max_attempts=3, delay=0.1, on=ConnectionError)
    def lookup_customer(email: str, api_key: str) -> dict[str, str]:
        if flaky["left"]:
            flaky["left"] -= 1
            raise ConnectionError("upstream hiccup")
        return {"plan": "pro", "contact": email}

    return ask, lookup_customer


def run_agent(ask: Any, lookup: Any) -> str:
    with genai.context(run_id="run-42", step=1):
        ask("charge card 4111 1111 1111 1111 for ada@example.com")
    with genai.context(run_id="run-42", step=2):
        lookup("ada@example.com", api_key="sk-live-abcdefghijklmnop")
    return "done"


def main() -> None:
    tape = AuditTape()
    events = []
    with fake_clock(), listening(events.append):
        ask, lookup = build(tape)
        run_agent(ask, lookup)

    llm, tool = tape.records()
    assert (llm.run_id, llm.step, llm.kind) == ("run-42", 1, "llm")
    assert (llm.input_tokens, llm.output_tokens) == (120, 40)
    assert llm.cost is not None and abs(llm.cost - (120 * 3 + 40 * 15) / 1e6) < 1e-12
    assert "4111" not in str(llm.input) and "ada@example.com" not in str(llm.input)
    assert tool.input is not None and tool.input["api_key"] == "***"
    assert [a["number"] for a in tool.attempts] == [1]  # the retry that preceded success
    assert [e.name for e in events if e.name.startswith("genai.")] == [
        "genai.llm_call",
        "genai.tool_call",
    ]

    # Replay: the same code path, served from the tape, no provider involved.
    with replay(tape) as provider:
        reply = provider.llm("any prompt")
        profile = provider.tool("lookup_customer")("any", api_key="x")
    assert reply["text"].startswith("echo: charge card")
    assert profile["plan"] == "pro"

    # A flow that diverges from the recording is caught.
    try:
        replay(tape).tool("lookup_customer")()
    except ReplayMismatch as exc:
        print("mismatch detected:", exc)
    else:
        raise AssertionError("expected a ReplayMismatch")
    print("recorded", len(tape), "calls; replay matched")


if __name__ == "__main__":
    main()
