"""Wiring: build the whole agent stack from settings and the fake systems."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from python_wrap import AuditTape, Redactor, ToolRegistry
from python_wrap.llm import Budget
from python_wrap.llm_testing import FakeProvider
from support.agent import AgentResult, run_agent
from support.backends import Mailbox, OrderBook, PaymentsApi
from support.config import Settings
from support.llm import make_ask, make_budget
from support.observability import make_redactor, make_tape
from support.provider import make_provider
from support.tools import build_registry


@dataclass
class SupportApp:
    settings: Settings
    primary: FakeProvider
    backup: FakeProvider
    orders: OrderBook
    payments: PaymentsApi
    mailbox: Mailbox
    budget: Budget
    tape: AuditTape
    redactor: Redactor
    registry: ToolRegistry
    ask: Any

    def handle(self, ticket: str, *, run_id: str) -> AgentResult:
        """Run one ticket as a guarded agent run."""
        with self.registry.run(run_id=run_id):
            return run_agent(ticket, ask=self.ask, dispatch=self.registry.call)


def build_app(
    settings: Settings,
    *,
    primary: FakeProvider | None = None,
    backup: FakeProvider | None = None,
) -> SupportApp:
    primary = primary or make_provider("primary")
    backup = backup or make_provider("backup")
    orders, payments, mailbox = OrderBook(), PaymentsApi(), Mailbox()
    redactor = make_redactor()
    tape = make_tape(settings.tape_path, redactor)
    budget = make_budget(settings)
    registry = build_registry(
        settings,
        orders=orders,
        payments=payments,
        mailbox=mailbox,
        tape=tape,
        redactor=redactor,
        budget=budget,
    )
    ask = make_ask(
        settings, primary=primary, backup=backup, budget=budget, tape=tape, redactor=redactor
    )
    return SupportApp(
        settings, primary, backup, orders, payments, mailbox, budget, tape, redactor, registry, ask
    )
