"""Policy explain/doctor findings and the opinionated preset policies."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from python_wrap import (
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    RateLimiter,
    RetryBudget,
    memoize,
    policy,
    retry,
    timeout,
)
from python_wrap.__main__ import main as cli
from python_wrap.doctor import Report, doctor, explain
from python_wrap.presets import db_call, http_client, third_party_api


def codes(report: Report) -> list[str]:
    return [finding.code for finding in report.findings]


def test_explain_lists_layers_in_canonical_order_with_settings():
    budget = RetryBudget()
    stack = policy(
        name="io",
        timeout=timeout(2),
        retry=retry(max_attempts=3, on=ConnectionError, budget=budget, max_time=30),
        breaker=CircuitBreaker(failure_threshold=4, name="io"),
        bulkhead=Bulkhead(5),
        limiter=RateLimiter(10, 1, name="io"),
    )
    report = stack.explain()
    assert [layer.role for layer in report.layers] == [
        "limiter",
        "bulkhead",
        "breaker",
        "retry",
        "timeout",
    ]
    assert report.layers[2].settings["failure_threshold"] == 4
    assert report.layers[3].settings["max_attempts"] == 3
    assert report.layers[4].settings == {"seconds": 2}
    assert report.ok and report.findings == ()
    text = str(report)
    assert text.startswith("io:") and "no findings" in text and "timeout(seconds=2)" in text


def test_unbounded_retry_is_an_error():
    report = explain(policy(retry=retry(max_attempts=None, on=OSError), timeout=timeout(1)))
    assert not report.ok
    assert "PW001" in codes(report)


def test_non_idempotent_retry_is_an_error_only_when_declared():
    stack = policy(
        retry=retry(max_attempts=3, on=OSError, budget=RetryBudget()), timeout=timeout(1)
    )
    assert stack.explain().ok
    report = stack.explain(idempotent=False)
    assert codes(report)[0] == "PW002"
    assert "ERROR PW002" in str(report)


def test_warnings_for_missing_timeout_broad_exceptions_and_budget():
    report = explain(policy(retry=retry(max_attempts=2)))
    assert report.ok  # warnings only
    assert {"PW003", "PW004", "PW008"} <= set(codes(report))
    narrowed = explain(policy(retry=retry(max_attempts=2, retry_if=lambda e: True)))
    assert "PW003" not in codes(narrowed)


def test_timeout_not_shorter_than_retry_budget():
    report = explain(
        policy(
            retry=retry(max_attempts=3, max_time=5, on=OSError, budget=RetryBudget()),
            timeout=timeout(5),
        )
    )
    assert codes(report) == ["PW005"]


def test_misplaced_layers_in_outer_inner_are_detected():
    # A breaker put *inside* the retry via inner= counts every attempt.
    report = explain(
        policy(
            retry=retry(max_attempts=2, on=OSError, budget=RetryBudget()),
            timeout=timeout(1),
            inner=CircuitBreaker(),
        )
    )
    assert "PW006" in codes(report)
    assert [layer.role for layer in report.layers] == ["retry", "timeout", "breaker"]


def test_duplicate_retry_layers_multiply_attempts():
    report = explain(
        policy(
            retry=[
                retry(max_attempts=3, on=OSError, budget=RetryBudget()),
                retry(max_attempts=3, on=OSError, budget=RetryBudget()),
            ],
            timeout=timeout(1),
        )
    )
    assert "PW007" in codes(report)


def test_doctor_walks_hand_stacked_decorators():
    @retry(max_attempts=3, on=ConnectionError, budget=RetryBudget())
    @timeout(2)
    @memoize
    def fetch(x: int) -> int:
        return x

    report = doctor(fetch)
    assert [layer.role for layer in report.layers] == ["retry", "timeout", "cache"]
    # A cache below the retry is legal but re-reads the cache on every attempt.
    assert report.ok and codes(report) == ["PW006"]
    assert fetch(2) == 2  # inspecting did not break anything


def test_doctor_flags_retry_outside_breaker_order():
    breaker = CircuitBreaker(name="b")

    @breaker
    @retry(max_attempts=2, on=OSError, budget=RetryBudget())
    @timeout(1)
    def ok() -> None: ...

    @retry(max_attempts=2, on=OSError, budget=RetryBudget())
    @breaker
    @timeout(1)
    def misordered() -> None: ...

    assert doctor(ok).findings == ()
    assert codes(doctor(misordered)) == ["PW006"]


def test_doctor_on_async_and_plain_functions():
    @retry(max_attempts=2, on=OSError, budget=RetryBudget())
    @timeout(1)
    async def fetch() -> int:
        return 1

    assert [layer.role for layer in doctor(fetch).layers] == ["retry", "timeout"]
    assert asyncio.run(fetch()) == 1

    def plain() -> None: ...

    report = explain(plain)
    assert report.layers == () and "no python-wrap layers" in str(report)


def test_presets_are_clean_and_ordered():
    for preset in (
        http_client("svc"),
        db_call("db"),
        third_party_api("vendor", calls=10, max_concurrent=4),
    ):
        report = preset.explain()
        assert report.findings == (), str(report)
    roles = [
        layer.role for layer in third_party_api("v", calls=1, max_concurrent=1).explain().layers
    ]
    assert roles == ["limiter", "bulkhead", "breaker", "retry", "timeout"]


def test_non_idempotent_presets_drop_the_retry_layer():
    for preset in (
        http_client("pay", idempotent=False),
        third_party_api("sms", idempotent=False),
        db_call("ledger", idempotent=False),
    ):
        report = preset.explain(idempotent=False)
        assert report.ok
        assert "retry" not in [layer.role for layer in report.layers]


def test_http_client_preset_retries_transport_errors_then_opens(fake_clock):
    calls: list[int] = []
    preset = http_client("flaky", timeout_seconds=1, attempts=3)

    @preset
    def call() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return "ok"

    assert call() == "ok"
    assert len(calls) == 3
    assert fake_clock.sleeps[:1] and fake_clock.sleeps[0] >= 0.2

    @http_client("down", attempts=1)
    def down() -> None:
        raise ConnectionError

    for _ in range(5):
        with pytest.raises(ConnectionError):
            down()
    with pytest.raises(CircuitOpenError):
        down()


def test_db_call_preset_retries_only_transient_errors(fake_clock):
    deadlock_type = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
    attempts: list[int] = []

    @db_call("orders")
    def save() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise deadlock_type(1213, "Deadlock found")
        return "saved"

    assert save() == "saved" and len(attempts) == 2

    @db_call("orders2")
    def bad() -> None:
        attempts.append(1)
        raise KeyError("bug")

    attempts.clear()
    with pytest.raises(KeyError):
        bad()
    assert attempts == [1]


def test_cli_doctor(capsys, monkeypatch):
    module = types.ModuleType("pw_doctor_target")
    module.good = http_client("good")  # type: ignore[attr-defined]
    module.bad = policy(retry=retry(max_attempts=None))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pw_doctor_target", module)
    assert cli(["doctor", "pw_doctor_target:good"]) == 0
    assert "good:" in capsys.readouterr().out
    assert cli(["doctor", "pw_doctor_target:bad"]) == 1
    assert "PW001" in capsys.readouterr().out
    assert cli(["doctor", "--strict", "pw_doctor_target:good"]) == 0
    assert cli(["doctor", "--non-idempotent", "pw_doctor_target:good"]) == 1
    assert cli(["nope"]) == 2
    assert cli([]) == 2
    assert cli(["--help"]) == 0
