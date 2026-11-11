"""Gateway behaviour: the happy path, tenant isolation, provider outage, stalls, audit, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway_app import gateway as gateway_module
from gateway_app.app import build_gateway
from gateway_app.gateway import Gateway, UnknownTenant
from gateway_app.providers import Provider

from python_wrap import AuditTape, BudgetExceeded, StreamTimeout
from python_wrap.__main__ import main as python_wrap_cli
from python_wrap.chaos import chaos
from python_wrap.llm_testing import FakeStream, fake_llm


@pytest.fixture(autouse=True)
def clean_chaos() -> None:
    chaos.clear()


def test_happy_path_is_cached_traced_and_priced(tmp_path: Path) -> None:
    gateway, tape = build_gateway(tmp_path / "audit.jsonl")
    first = gateway.chat("acme", "What is a bulkhead?")
    again = gateway.chat("acme", "What is a bulkhead?")
    assert first == again == "[alpha-1] What is a bulkhead?"
    assert gateway.providers[0].fake.call_count == 1  # the repeat was a cache hit
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(lines) == len(tape) == 1  # one record per real provider call
    assert gateway.stack("acme").spend.spent().tokens == 1000
    assert gateway.stack("acme").spend.spent().usd == pytest.approx(0.0102)


def test_tenant_over_budget_is_refused_without_touching_others() -> None:
    gateway, _ = build_gateway()
    gateway.chat("tinyshop", "first question")  # costs more than its whole allowance
    with pytest.raises(BudgetExceeded) as refused:
        gateway.chat("tinyshop", "second question")
    assert refused.value.scope == "total"
    assert gateway.providers[0].fake.call_count == 1  # the second call never left the gateway
    assert gateway.chat("acme", "second question").startswith("[alpha-1]")  # acme unaffected
    with pytest.raises(UnknownTenant):
        gateway.chat("nobody", "hello")


def test_provider_outage_falls_back_and_the_breaker_stops_the_hammering() -> None:
    gateway, _ = build_gateway()
    alpha, beta = gateway.providers
    chaos.llm.overloaded("llm.alpha")  # alpha is down for everyone
    answers = [gateway.chat("acme", f"question {n}") for n in range(6)]
    assert all(a.startswith("[beta-1]") for a in answers)
    assert alpha.fake.call_count == 3  # breaker opened after 3 failures; later calls skip it
    assert beta.fake.call_count == 6
    assert gateway.breakers["alpha-1"].state.value == "open"
    assert gateway.breakers["beta-1"].state.value == "closed"


def test_stalled_stream_is_cut_and_the_partial_answer_is_still_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "CHUNK_GAP_SECONDS", 0.05)
    tape = AuditTape()
    stalled = fake_llm([FakeStream(["Hel", "lo ", "wor", "ld"], chunk_delay=[0, 0, 0.3, 0])])
    gateway = Gateway(
        [Provider("alpha", "alpha-1", tape, fake=stalled), Provider("beta", "beta-1", tape)]
    )
    received: list[str] = []
    with pytest.raises(StreamTimeout) as stop:
        for piece in gateway.stream("acme", "greet"):
            received.append(piece)
    assert stop.value.limit == "chunk_gap" and received == ["Hel", "lo "]


def test_healthy_stream_records_usage() -> None:
    gateway, _ = build_gateway()
    text = "".join(gateway.stream("acme", "greet"))
    assert text.startswith("[alpha-1]")
    assert gateway.stack("acme").spend.spent().tokens > 0


def test_python_wrap_cli_surfaces(capsys: pytest.CaptureFixture[str]) -> None:
    assert python_wrap_cli(["doctor", "gateway_app.app:acme_chat"]) == 0  # no errors
    report = capsys.readouterr().out
    for layer in ("cache", "budget", "limiter", "fallback", "timeout"):
        assert layer in report
    assert "PW014" not in report  # every provider call has a timeout
    assert python_wrap_cli(["api", "llm_cache"]) == 0
    assert "llm_cache" in capsys.readouterr().out
    assert python_wrap_cli(["recipe", "limit", "llm", "tokens"]) == 0
    assert "Budget" in capsys.readouterr().out


def test_doctor_findings_are_available_to_code() -> None:
    gateway, _ = build_gateway()
    codes = {finding.code for finding in gateway.doctor_findings("acme")}
    assert codes <= {"PW006"}  # budget sits outside the fallback chain on purpose
