"""Story: the doctor approves a sound stack and flags an unsafe one."""

from __future__ import annotations

import pytest

from python_wrap import CircuitBreaker, RetryBudget, policy, retry, timeout
from python_wrap.doctor import explain

pytestmark = pytest.mark.e2e


def good_policy() -> object:
    return policy(
        name="catalog",
        breaker=CircuitBreaker(failure_threshold=5, on=ConnectionError),
        retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
        timeout=timeout(2),
    )


def test_a_sound_stack_is_explained_layer_by_layer() -> None:
    stack = policy(
        name="catalog",
        retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
        timeout=timeout(2),
    )
    report = stack.explain()
    assert report.ok
    assert [layer.role for layer in report.layers] == ["retry", "timeout"]
    assert "catalog" in str(report)

    @stack
    def fetch() -> str:
        return "ok"

    assert fetch() == "ok"
    assert explain(fetch).ok


def test_retrying_a_non_idempotent_call_is_an_error() -> None:
    stack = policy(
        name="charge",
        retry=retry(max_attempts=3, on=ConnectionError),
        timeout=timeout(5),
    )
    assert stack.explain(idempotent=True).ok
    report = stack.explain(idempotent=False)
    assert not report.ok
    assert "PW002" in [finding.code for finding in report.findings]
    assert "PW002" in str(report)
