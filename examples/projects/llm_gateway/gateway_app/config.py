"""Tenant plans and the price table (invented names and numbers)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantPlan:
    usd: float  # spend allowance for the billing period
    tokens_per_minute: int  # the tenant's share of the provider quota


PLANS = {
    "acme": TenantPlan(usd=5.0, tokens_per_minute=20_000),
    "tinyshop": TenantPlan(usd=0.001, tokens_per_minute=20_000),
}

# model -> (USD per million input tokens, USD per million output tokens)
PRICES = {"alpha-1": (3.0, 15.0), "beta-1": (0.5, 2.0)}

FIRST_TOKEN_SECONDS = 2.0
CHUNK_GAP_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 30.0
