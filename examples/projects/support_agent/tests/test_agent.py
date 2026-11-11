from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support.agent import run_agent
from support.app import SupportApp

from conftest import TICKET
from python_wrap import (
    ReplayMismatch,
    RunBudgetExceeded,
    StepLimitExceeded,
    replay,
)
from python_wrap.chaos import chaos
from python_wrap.doctor import explain
from python_wrap.llm import BudgetExceeded

MakeApp = Callable[..., SupportApp]


def test_happy_path_refunds_and_emails(make_app: MakeApp) -> None:
    app = make_app()
    result = app.handle(TICKET, run_id="t-1")

    assert result.tool_calls == ["lookup_order", "refund", "send_email"]
    assert app.payments.refunds == [{"order_id": "A-1001", "cents": 4200, "reason": "damaged"}]
    assert [m["to"] for m in app.mailbox.sent] == ["pat@example.com"]
    assert result.llm_calls == 4 and app.primary.call_count == 4
    assert app.primary.total_tokens == 600  # 4 calls x (120 in + 30 out)


def test_a_repeated_run_does_not_refund_or_email_twice(make_app: MakeApp) -> None:
    app = make_app()
    app.handle(TICKET, run_id="t-1")
    app.handle(TICKET, run_id="t-1")  # e.g. the job queue delivered the ticket twice
    assert len(app.payments.refunds) == 1
    assert len(app.mailbox.sent) == 1


def test_refund_is_once_per_order_even_in_a_new_run(make_app: MakeApp) -> None:
    app = make_app()
    app.handle(TICKET, run_id="t-1")
    app.handle(TICKET, run_id="t-2")  # a different run, same order: the business key wins
    assert len(app.payments.refunds) == 1


def test_large_refund_is_denied_and_the_run_still_finishes(make_app: MakeApp) -> None:
    app = make_app()
    result = app.handle("Order A-1002 is broken, I am lee@example.com", run_id="t-1")
    assert result.denied == ["refund"]
    assert app.payments.refunds == []
    assert result.text  # the agent answered instead of crashing


def test_tape_is_redacted_saved_and_replayable(make_app: MakeApp, tmp_path: Path) -> None:
    app = make_app()
    original = app.handle(TICKET, run_id="t-1")
    path = tmp_path / "tape.jsonl"

    text = path.read_text(encoding="utf-8")
    assert "pat@example.com" not in text
    assert "[redacted:email]" in text

    mailbox_before = len(app.mailbox.sent)
    with replay(path) as provider:  # asserts every recorded call is made, in order
        replayed = run_agent(
            TICKET, ask=provider.llm, dispatch=lambda name, args: provider.tool(name)()
        )
    assert replayed.text == original.text
    assert replayed.tool_calls == original.tool_calls
    assert len(app.mailbox.sent) == mailbox_before  # replay touched no real system


def test_replay_notices_when_the_flow_changes(make_app: MakeApp, tmp_path: Path) -> None:
    app = make_app()
    app.handle(TICKET, run_id="t-1")
    provider = replay(tmp_path / "tape.jsonl")
    provider.llm([])  # the recording starts with a model call...
    with pytest.raises(ReplayMismatch):
        provider.llm([])  # ...but the second recorded call was a tool call


def test_token_budget_stops_a_runaway_run(make_app: MakeApp) -> None:
    """The failure scenario: 150 tokens per call, 200 allowed, so the run is cut short."""
    app = make_app(token_budget=200)
    with pytest.raises((RunBudgetExceeded, BudgetExceeded)):
        app.handle(TICKET, run_id="t-1")
    assert app.primary.call_count <= 2
    assert app.mailbox.sent == []  # never reached the email step


def test_step_limit_stops_the_loop(make_app: MakeApp) -> None:
    app = make_app(max_steps=3)
    with pytest.raises(StepLimitExceeded):
        app.handle(TICKET, run_id="t-1")
    assert app.mailbox.sent == []


def test_rate_limited_primary_falls_back_to_the_backup(make_app: MakeApp) -> None:
    chaos.llm.rate_limit("llm.primary", times=1, retry_after=1)
    app = make_app()
    result = app.handle(TICKET, run_id="t-1")

    assert result.text.startswith("Handled A-1001")
    assert app.backup.served_count == 1  # one turn, served by the backup model
    assert app.primary.served_count == 3
    assert len(app.payments.refunds) == 1


def test_both_models_rate_limited_retries_after_retry_after(make_app: MakeApp, clock: Any) -> None:
    chaos.llm.rate_limit("llm.*", times=2, retry_after=7)
    app = make_app()
    result = app.handle(TICKET, run_id="t-1")

    assert result.llm_calls == 4
    assert clock.sleeps[0] == 7.0  # the provider's Retry-After, honoured, instantly
    assert len(app.payments.refunds) == 1


def test_doctor_is_happy_with_the_stack(make_app: MakeApp) -> None:
    app = make_app()
    llm_report = explain(app.ask)
    assert llm_report.ok and llm_report.findings == (), str(llm_report)
    assert [layer.role for layer in llm_report.layers] == ["budget", "breaker", "retry", "timeout"]

    tools_report = explain(app.registry)
    assert tools_report.ok and tools_report.findings == (), str(tools_report)
