"""Wiring: build the providers, the audit tape and the gateway."""

from __future__ import annotations

from pathlib import Path

from python_wrap import AuditTape

from .gateway import Gateway
from .providers import Provider


def build_gateway(tape_path: str | Path | None = None) -> tuple[Gateway, AuditTape]:
    """Create a gateway; with ``tape_path`` the audit tape is a JSONL file (one call per line)."""
    tape = AuditTape(tape_path)  # a ".jsonl" path selects the JSONL backend
    providers = [Provider("alpha", "alpha-1", tape), Provider("beta", "beta-1", tape)]
    return Gateway(providers), tape


# A module-level handler so that `python -m python_wrap doctor gateway_app.app:acme_chat` works.
gateway, tape = build_gateway()
acme_chat = gateway.stack("acme").handler
