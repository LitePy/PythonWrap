"""Reference data that changes rarely: fetched once, then served from memory."""

from __future__ import annotations

from python_wrap import memoize

lookups = {"count": 0}


@memoize(ttl=300)
def exchange_rates() -> dict[str, float]:
    """Pretend this is a slow call to a rates service."""
    lookups["count"] += 1
    return {"EUR": 1.0, "USD": 1.08}
