"""Optional Redis cache adapter. The caller supplies a client; redis is not imported."""

from __future__ import annotations

import logging
import math
import pickle
import re
from collections.abc import Callable, Hashable
from typing import Any

from python_wrap._runtime import digest

__all__ = ["RedisStore"]


class RedisStore:
    """A namespaced `CacheStore` with opaque hashed keys and configurable serialization.

    Args:
        client: A synchronous Redis-compatible client.
        prefix: A nonempty namespace unique to one cached function.
        dumps: Serializer for entries; defaults to pickle (trusted Redis data only).
        loads: Deserializer matching dumps.
        key_format: Optional function turning a cache key into a stable external key.
        fail_open: Treat backend errors as misses/no-ops and log them, rather than raise.

    `size()` is unknown (None); `cache_info()` never scans Redis. `clear()` scans only
    this namespace, escaping glob metacharacters, and deletes in bounded batches.
    Async memoize currently calls CacheStore methods synchronously; use a local cache
    on latency-sensitive event loops or supply a fast local adapter.
    """

    def __init__(
        self,
        client: Any,
        prefix: str,
        *,
        dumps: Callable[[Any], bytes] = pickle.dumps,
        loads: Callable[[bytes], Any] = pickle.loads,
        key_format: Callable[[Hashable], str] | None = None,
        fail_open: bool = True,
    ) -> None:
        if not prefix:
            raise ValueError("RedisStore needs a nonempty per-function prefix")
        self.client, self.prefix = client, prefix
        self.dumps, self.loads = dumps, loads
        self.key_format, self.fail_open = key_format, fail_open

    def _key(self, key: Hashable) -> str:
        return self.prefix + (self.key_format(key) if self.key_format else digest(key))

    def _run(self, action: Callable[[], Any], default: Any = None) -> Any:
        try:
            return action()
        except Exception:
            if not self.fail_open:
                raise
            logging.getLogger("python_wrap").warning("RedisStore operation failed", exc_info=True)
            return default

    def get(self, key: Hashable, /) -> Any:
        """Return a decoded entry, or None on a miss."""

        def read() -> Any:
            value = self.client.get(self._key(key))
            return None if value is None else self.loads(value)

        return self._run(read)

    def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        """Store an entry with millisecond TTL precision."""
        if ttl is not None and ttl <= 0:
            self.delete(key)
            return
        options = {} if ttl is None else {"px": max(1, math.ceil(ttl * 1000))}
        self._run(lambda: self.client.set(self._key(key), self.dumps(entry), **options))

    def delete(self, key: Hashable, /) -> bool:
        """Delete one entry; report whether it existed."""
        return bool(self._run(lambda: self.client.delete(self._key(key)), False))

    def clear(self) -> None:
        """Delete this namespace, in batches, using UNLINK where supported."""

        def clear() -> None:
            pattern = re.sub(r"([\\*?\[\]])", r"\\\1", self.prefix) + "*"
            batch = []
            delete = getattr(self.client, "unlink", None) or self.client.delete
            for key in self.client.scan_iter(match=pattern, count=200):
                batch.append(key)
                if len(batch) == 200:
                    delete(*batch)
                    batch.clear()
            if batch:
                delete(*batch)

        self._run(clear)

    def size(self) -> None:
        """Return None: an exact distributed entry count is intentionally not scanned."""
        return None

    def __len__(self) -> int:
        """Return zero for compatibility; use size() for the unknown-size contract."""
        return 0
