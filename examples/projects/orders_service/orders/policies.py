"""Resilience policies, built by functions so every app (and every test) gets fresh state."""

from __future__ import annotations

from orders.config import Settings
from python_wrap import Policy
from python_wrap.presets import http_client


def payments_policy(settings: Settings) -> Policy:
    """Timeout, retry with backoff and a circuit breaker for the payment gateway.

    ``idempotent=True`` is honest here: the client always sends an idempotency key, so a
    repeated charge request cannot charge twice. Without that, retries would be unsafe.
    """
    return http_client(
        "payments-gateway",
        timeout_seconds=settings.payment_timeout,
        attempts=settings.payment_attempts,
        idempotent=True,
    )
