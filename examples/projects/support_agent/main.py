"""Run the support agent end to end against a scripted fake model: ``python main.py``."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support.agent import run_agent
from support.app import build_app
from support.config import Settings

from python_wrap import replay
from python_wrap.chaos import chaos
from python_wrap.doctor import explain
from python_wrap.testing import fake_clock

TICKET = "My order A-1001 arrived broken, I am pat@example.com"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp, fake_clock():

        def settings(name: str) -> Settings:
            return Settings(tape_path=os.path.join(tmp, f"{name}.jsonl"))

        # 1. A normal ticket: lookup, approved refund, email, final answer.
        app = build_app(settings("main"))
        result = app.handle(TICKET, run_id="t-1")
        print(f"1. {result.text}")
        print(f"   tools: {result.tool_calls}; refunds: {app.payments.refunds}")
        print(f"   tokens: {app.primary.total_tokens}; tape records: {len(app.tape)}")

        # 2. The tape is redacted, and replays the run with no model and no side effects.
        with open(settings("main").tape_path, encoding="utf-8") as handle:
            assert "pat@example.com" not in handle.read()
        with replay(settings("main").tape_path) as provider:
            again = run_agent(
                TICKET, ask=provider.llm, dispatch=lambda name, args: provider.tool(name)()
            )
        assert again.text == result.text
        print(f"2. replayed from the redacted tape without a model: {again.text!r}")

        # 3. The same run id again (a retried job): nothing is refunded or mailed twice.
        app.handle(TICKET, run_id="t-1")
        print(
            f"3. after a repeat: {len(app.payments.refunds)} refund, {len(app.mailbox.sent)} email"
        )

        # 4. A large refund needs a human: the approver says no and the agent carries on.
        big = build_app(settings("big"))
        denied = big.handle("Order A-1002 is broken, I am lee@example.com", run_id="t-2")
        print(f"4. large refund denied: {denied.denied}; refunds made: {big.payments.refunds}")

        # 5. The primary model is rate limited: the backup model answers.
        chaos.llm.rate_limit("llm.primary", times=1, retry_after=1)
        try:
            flaky = build_app(settings("flaky"))
            flaky.handle(TICKET, run_id="t-3")
        finally:
            chaos.clear()
        print(f"5. primary rate limited once; backup served {flaky.backup.served_count} call(s)")

        # 6. doctor on the stack.
        for label, target in (("llm client", app.ask), ("tool registry", app.registry)):
            report = explain(target)
            print(
                f"6. doctor {label}: ok={report.ok}, findings={[f.code for f in report.findings]}"
            )


if __name__ == "__main__":
    main()
