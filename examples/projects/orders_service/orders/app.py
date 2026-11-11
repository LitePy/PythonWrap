"""Wiring: build every component once and hand them around explicitly.

Everything stateful (caches, breakers, rate-limit windows, the idempotency store) is created
inside ``build_app``, so two apps never share state and tests start clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orders.config import Settings
from orders.fakes import FakeInventoryDb, FakeMailer, FakePaymentGateway
from orders.inventory import Inventory
from orders.outbox import close_outbox, make_outbox
from orders.payments import make_charge
from orders.policies import payments_policy
from orders.service import build_create_order
from python_wrap import Buffered, HealthRegistry, Policy

PRODUCTS = {"mug": (50, 1200), "poster": (5, 2500)}  # sku -> (stock, price in cents)


@dataclass
class App:
    settings: Settings
    gateway: FakePaymentGateway
    db: FakeInventoryDb
    mailer: FakeMailer
    payments: Policy
    inventory: Inventory
    outbox: Buffered
    create_order: Any
    health: HealthRegistry

    def close(self) -> None:
        close_outbox(self.outbox)


def build_app(
    settings: Settings,
    *,
    gateway: FakePaymentGateway | None = None,
    db: FakeInventoryDb | None = None,
    mailer: FakeMailer | None = None,
) -> App:
    gateway = gateway or FakePaymentGateway()
    db = db or FakeInventoryDb(PRODUCTS)
    mailer = mailer or FakeMailer()

    payments = payments_policy(settings)
    inventory = Inventory(db, settings.stock_ttl)
    outbox = make_outbox(settings.outbox_path, mailer, settings.outbox_flush_interval)
    create_order = build_create_order(settings, inventory, make_charge(gateway, payments), outbox)

    # Health: structural state only, nothing here calls the dependencies except the
    # explicit checks we register ourselves.
    health = HealthRegistry()
    health.register_check("payments", gateway.ping)
    health.register("inventory_cache", inventory.lookup)
    health.register_check(
        "email_outbox",
        lambda: {
            "healthy": outbox.stats().failed_batches == 0,
            "queued": outbox.stats().queued,
        },
    )
    return App(settings, gateway, db, mailer, payments, inventory, outbox, create_order, health)
