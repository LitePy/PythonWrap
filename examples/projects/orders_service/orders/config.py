"""Settings, read once at start-up. Every knob has a default so the demo needs no setup."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Settings:
    payment_attempts: int = 3
    payment_timeout: float = 2.0
    stock_ttl: float = 30.0
    orders_per_minute: int = 3
    outbox_path: str = "outbox.db"
    outbox_flush_interval: float = 0.05

    @classmethod
    def from_env(cls) -> Settings:
        """Read ``ORDERS_*`` environment variables on top of the defaults."""
        env = os.environ
        settings = cls()
        if "ORDERS_PAYMENT_ATTEMPTS" in env:
            settings = replace(settings, payment_attempts=int(env["ORDERS_PAYMENT_ATTEMPTS"]))
        if "ORDERS_PER_MINUTE" in env:
            settings = replace(settings, orders_per_minute=int(env["ORDERS_PER_MINUTE"]))
        if "ORDERS_OUTBOX_PATH" in env:
            settings = replace(settings, outbox_path=env["ORDERS_OUTBOX_PATH"])
        return settings
