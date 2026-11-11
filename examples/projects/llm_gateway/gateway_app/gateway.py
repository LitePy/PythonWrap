"""The gateway: one resilient entry point for every tenant."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from python_wrap import (
    Budget,
    BudgetExceeded,
    CircuitBreaker,
    fallback_models,
    llm_cache,
    stream_timeout,
    token_rate_limit,
)
from python_wrap.doctor import Finding, doctor

from .config import CHUNK_GAP_SECONDS, FIRST_TOKEN_SECONDS, PLANS, PRICES
from .providers import Provider


class UnknownTenant(KeyError):
    pass


@dataclass
class TenantStack:
    """Everything that belongs to one tenant: its allowance, its quota share and its cache."""

    spend: Budget
    handler: Callable[..., Any]


class Gateway:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers
        # One breaker per provider, shared by all tenants: an outage is everyone's outage.
        self.breakers = {
            p.model: CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, name=p.name)
            for p in providers
        }
        self.chain = fallback_models(
            [p.complete for p in providers],
            names=[p.model for p in providers],
            breaker=self.breakers,
            name="gateway",
        )
        self._stacks: dict[str, TenantStack] = {}
        self._lock = threading.Lock()

    def stack(self, tenant: str) -> TenantStack:
        with self._lock:
            if tenant not in self._stacks:
                plan = PLANS.get(tenant)
                if plan is None:
                    raise UnknownTenant(tenant)
                spend = Budget(usd=plan.usd, prices=PRICES, name=f"spend-{tenant}")
                quota = token_rate_limit(plan.tokens_per_minute, block=False, name=f"tpm-{tenant}")
                # Outermost first: a cache hit costs no tokens, no quota and no money.
                handler = llm_cache(ttl=600)(spend(quota(self.chain)))
                self._stacks[tenant] = TenantStack(spend, handler)
            return self._stacks[tenant]

    def chat(self, tenant: str, prompt: str, max_tokens: int = 256) -> str:
        messages = [{"role": "user", "content": prompt}]
        response = self.stack(tenant).handler(messages=messages, max_tokens=max_tokens)
        return str(response.text)

    def stream(self, tenant: str, prompt: str) -> Iterator[str]:
        """Stream text; a stalled provider raises `StreamTimeout` instead of hanging."""
        stack = self.stack(tenant)
        if stack.spend.exhausted():  # streams bypass the decorator, so check by hand
            spent = stack.spend.spent()
            raise BudgetExceeded(
                f"tenant {tenant} is out of budget",
                budget=stack.spend.name,
                scope="total",
                tokens=spent.tokens,
                usd=spent.usd,
                limit_tokens=None,
                limit_usd=PLANS[tenant].usd,
            )
        messages = [{"role": "user", "content": prompt}]
        last: Exception | None = None
        for provider in self.providers:
            try:
                source = provider.stream(messages=messages)
            except Exception as exc:
                last = exc
                continue
            guarded = stream_timeout(
                iter(source), first_token=FIRST_TOKEN_SECONDS, chunk_gap=CHUNK_GAP_SECONDS
            )
            return self._metered(stack, guarded, provider.model)
        assert last is not None
        raise last

    @staticmethod
    def _metered(stack: TenantStack, chunks: Any, model: str) -> Iterator[str]:
        usage = None
        try:
            for chunk in chunks:
                usage = chunk.usage or usage
                yield str(chunk.text)
        finally:
            if usage is not None:
                stack.spend.record(
                    SimpleNamespace(model=model, usage=usage)
                )  # the last chunk has the real usage

    def doctor_findings(self, tenant: str = "acme") -> list[Finding]:
        """Lint the tenant's stack the way `python -m python_wrap doctor` does."""
        return list(doctor(self.stack(tenant).handler).findings)
