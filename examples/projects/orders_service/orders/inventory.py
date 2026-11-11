"""Stock and price lookups: cached for a short time, one database query per miss."""

from __future__ import annotations

from typing import Any

from python_wrap import memoize, singleflight


class OutOfStock(Exception):
    def __init__(self, sku: str, wanted: int, available: int) -> None:
        super().__init__(f"{sku}: wanted {wanted}, only {available} left")
        self.sku = sku


class Inventory:
    def __init__(self, db: Any, ttl: float) -> None:
        self.db = db

        # memoize outside, singleflight inside: a cache hit never touches the lock, and
        # when 50 requests miss together only one of them queries the database.
        @memoize(ttl=ttl, maxsize=1000)
        @singleflight
        def lookup(sku: str) -> dict[str, int]:
            info: dict[str, int] = self.db.lookup(sku)
            return info

        self.lookup = lookup

    def require(self, sku: str, quantity: int) -> int:
        """Return the unit price in cents, or raise `OutOfStock`."""
        info = self.lookup(sku)
        if info["stock"] < quantity:
            raise OutOfStock(sku, quantity, info["stock"])
        return info["price_cents"]

    def reserve(self, sku: str, quantity: int) -> None:
        self.db.decrement(sku, quantity)
        self.lookup.cache_clear()  # the cached stock level is now wrong
