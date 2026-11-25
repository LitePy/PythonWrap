"""Presets, retry budgets and PolicyRegistry accept shared Redis state."""

from __future__ import annotations

from typing import Any

from python_wrap.config import configured
from python_wrap.policies import PolicyRegistry
from python_wrap.presets import http_client, third_party_api
from python_wrap.retry import RetryBudget
from python_wrap.shared import RedisRetryBudget, SharedBackends


class FakeRedis:
    """Understands only the budget script; records every other eval."""

    def __init__(self) -> None:
        self.req = self.ret = 0
        self.other: list[Any] = []

    def eval(self, script: str, numkeys: int, key: str, *args: Any) -> Any:
        if "'deposit'" not in script:
            self.other.append(key)
            return [b"closed"]
        op, _width, ratio, reserve = args[0], args[1], float(args[2]), float(args[3])
        if op == "deposit":
            self.req += 1
            return 1
        if op == "totals":
            return [self.req, self.ret]
        if ratio * self.req + reserve - self.ret >= 1:
            self.ret += 1
            return 1
        return 0


def test_retry_budget_backend_is_shared_between_budgets() -> None:
    client = FakeRedis()
    a = RetryBudget(ratio=0, min_per_second=0.2, window=10, backend=RedisRetryBudget(client, "v"))
    b = RetryBudget(ratio=0, min_per_second=0.2, window=10, backend=RedisRetryBudget(client, "v"))
    assert a.try_withdraw() and b.try_withdraw()  # reserve of 2 retries, drawn by two budgets
    assert not a.try_withdraw()
    assert b.stats.retries == 2 and a.stats.rejected == 1


def test_preset_uses_shared_budget_and_breaker_backend() -> None:
    client = FakeRedis()
    pol = http_client("g2-http", shared=client)
    layers = {layer.role: layer for layer in pol.explain().layers}
    assert layers["breaker"].settings["shared"] is True


def test_third_party_window_and_configure_default() -> None:
    client = FakeRedis()
    with configured(shared=SharedBackends(client=client)):
        pol = third_party_api("g2-vendor", calls=30, max_concurrent=3)
        layers = {layer.role: layer for layer in pol.explain().layers}
        assert all(layers[r].settings["shared"] for r in ("breaker", "limiter", "bulkhead"))
        registry = PolicyRegistry()
        registry.register("g2-reg", breaker={"failure_threshold": 2})
        assert registry._compiled["g2-reg"].explain().layers[0].settings["shared"] is True


def test_unshared_preset_still_per_process() -> None:
    layers = {layer.role: layer for layer in http_client("g2-local").explain().layers}
    assert layers["breaker"].settings["shared"] is False
