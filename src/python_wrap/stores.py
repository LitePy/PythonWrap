"""Optional Redis cache adapter. The caller supplies a client; redis is not imported."""

from __future__ import annotations

import hashlib
import hmac
import math
import pickle
import re
import warnings
from collections.abc import Callable, Hashable
from typing import Any

from python_wrap._backend import BackendMonitor
from python_wrap._runtime import stable_digest

KEY_FORMAT = "k2:"  # key encoding version: entries written by 0.4.0b1 expire unread

__all__ = ["RedisStore", "json_codec", "signed_pickle_codec"]


class InvalidSignature(ValueError):
    """A cache entry failed authentication."""


def signed_pickle_codec(secret: bytes) -> tuple[Callable[[Any], bytes], Callable[[bytes], Any]]:
    """Authenticate pickle bytes before loading them; keep the signing key private."""
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise ValueError("secret must be at least 16 bytes")

    def dumps(value: Any) -> bytes:
        payload = pickle.dumps(value)
        return hmac.digest(secret, payload, hashlib.sha256) + payload

    def loads(data: bytes) -> Any:
        signature, payload = data[:32], data[32:]
        if not hmac.compare_digest(signature, hmac.digest(secret, payload, hashlib.sha256)):
            raise InvalidSignature("cache entry signature is invalid")
        return pickle.loads(payload)

    return dumps, loads


def json_codec() -> tuple[Callable[[Any], bytes], Callable[[bytes], Any]]:
    """Return a JSON codec for cache entries with JSON-compatible values."""
    import json

    return lambda value: json.dumps(value, separators=(",", ":")).encode(), json.loads


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
    `memoize` on an ``async def`` function calls CacheStore methods synchronously, so
    every Redis round trip would block the event loop. For async code use
    ``amemoize(store=ThreadedStore(RedisStore(...)))`` (round trips in worker threads)
    or `python_wrap.async_cache.AsyncRedisStore` with a ``redis.asyncio`` client.
    """

    def __init__(
        self,
        client: Any,
        prefix: str,
        *,
        dumps: Callable[[Any], bytes] | None = None,
        loads: Callable[[bytes], Any] | None = None,
        key_format: Callable[[Hashable], str] | None = None,
        fail_open: bool = True,
        retry_backend_after: float = 1.0,
        version: int | None = None,
    ) -> None:
        if not prefix:
            raise ValueError("RedisStore needs a nonempty per-function prefix")
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ValueError("version must be a positive integer")
        if dumps is None and loads is None:
            from python_wrap.config import get_config

            codec = get_config().get("cache_codec")
            if codec is None:
                warnings.warn(
                    "implicit pickle cache codec is deprecated; pass a codec",
                    DeprecationWarning,
                    stacklevel=2,
                )
                codec = (pickle.dumps, pickle.loads)
            dumps, loads = codec
        self.client, self.prefix = client, prefix + (f"v{version}:" if version else "")
        self.dumps, self.loads = dumps or pickle.dumps, loads or pickle.loads
        self.key_format, self.fail_open = key_format, fail_open
        # fail_open=True is "allow" (a miss); False is "raise".
        self._monitor = BackendMonitor(
            "cache", prefix, "allow" if fail_open else "raise", retry_backend_after
        )

    def _key(self, key: Hashable) -> str:
        if self.key_format:
            return self.prefix + self.key_format(key)
        return self.prefix + KEY_FORMAT + stable_digest(key)

    def _run(self, action: Callable[[], Any], default: Any = None) -> Any:
        return self._monitor.call("cache", action, lambda: default)

    def get(self, key: Hashable, /) -> Any:
        """Return a decoded entry, or None on a miss."""

        def read() -> Any:
            value = self.client.get(self._key(key))
            if value is None:
                return None
            try:
                return self.loads(value)
            except InvalidSignature:
                from python_wrap import _core

                _core.emit("cache.rejected", self.prefix, reason="signature")
                return None

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
