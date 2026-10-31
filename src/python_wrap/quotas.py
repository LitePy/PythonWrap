"""Atomic Redis rate windows and explicit run budgets."""

from __future__ import annotations

import asyncio
import functools
import math
import uuid
from collections.abc import Callable
from typing import Any, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap._runtime import digest, seconds
from python_wrap.errors import RateLimitExceeded

__all__ = ["Quota", "QuotaExhausted", "RedisWindow", "shared_rate_limit"]
F = TypeVar("F", bound=Callable[..., Any])


class QuotaExhausted(RuntimeError):
    """A named run quota has no remaining allowance."""


class Quota:
    """A thread-safe explicit run budget; optionally shared via a Redis counter.

    Redis quotas need a unique key per run, not one forever-growing global key. An
    optional TTL bounds retention; reset is an explicit administrative operation.
    """

    def __init__(
        self,
        limit: int,
        name: str = "quota",
        *,
        client: Any = None,
        key: str | None = None,
        ttl: float | str | None = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be nonnegative")
        if client is not None and limit > 2**53 - 1:
            raise ValueError("Redis quota limit exceeds exact Lua integer range")
        if key is not None and not key:
            raise ValueError("quota key must not be empty")
        self.limit, self.name, self.client = limit, name, client
        self.key = key or f"python-wrap:quota:{name}"
        self.ttl = None if ttl is None else seconds(ttl)
        self._used = 0
        self._lock = _fork.ForkSafeLock()

    def take(self, amount: int = 1) -> int:
        """Consume an amount atomically and return remaining allowance."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("amount must be nonnegative")
        if self.client is not None and amount > 2**53 - 1:
            raise ValueError("Redis quota amount exceeds exact Lua integer range")
        if self.client is None:
            with self._lock:
                if self._used + amount > self.limit:
                    raise QuotaExhausted(self.name)
                self._used += amount
                return self.limit - self._used
        script = """
local used=tonumber(redis.call('GET',KEYS[1]) or '0')
if used+tonumber(ARGV[1])>tonumber(ARGV[2]) then return -1 end
used=redis.call('INCRBY',KEYS[1],ARGV[1])
if tonumber(ARGV[3])>0 and redis.call('PTTL',KEYS[1])<0 then
 redis.call('PEXPIRE',KEYS[1],ARGV[3]) end
return tonumber(ARGV[2])-used
"""
        result = int(
            self.client.eval(
                script,
                1,
                self.key,
                amount,
                self.limit,
                0 if self.ttl is None else math.ceil(self.ttl * 1000),
            )
        )
        if result < 0:
            raise QuotaExhausted(self.name)
        return result

    @property
    def remaining(self) -> int:
        if self.client is None:
            with self._lock:
                used = self._used
        else:
            used = int(self.client.get(self.key) or 0)
        return max(0, self.limit - used)

    def reset(self) -> None:
        if self.client is None:
            with self._lock:
                self._used = 0
        else:
            self.client.delete(self.key)


_WINDOW = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local horizon=now-tonumber(ARGV[2]); redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',horizon)
if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[1]) then
 local first=redis.call('ZRANGE',KEYS[1],0,0,'WITHSCORES')
 return math.max(1,tonumber(first[2])+tonumber(ARGV[2])-now)
end
redis.call('ZADD',KEYS[1],now,ARGV[3]); redis.call('PEXPIRE',KEYS[1],ARGV[2]); return 0
"""


class RedisWindow:
    """Atomic sliding-window admission using the Redis server's clock."""

    def __init__(self, client: Any, key: str, *, calls: int, period: float | str) -> None:
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 1 or not key:
            raise ValueError("calls must be positive and key nonempty")
        if calls > 2**53 - 1:
            raise ValueError("Redis window capacity exceeds exact Lua integer range")
        self.client, self.key, self.calls = client, key, calls
        self.period = seconds(period)

    def acquire(self, key: Any = None) -> float:
        """Return zero when admitted, otherwise seconds until the oldest slot expires."""
        return (
            float(
                self.client.eval(
                    _WINDOW,
                    1,
                    self.key + ":" + digest(key),
                    self.calls,
                    max(1, math.ceil(self.period * 1000)),
                    uuid.uuid4().hex,
                )
            )
            / 1000
        )

    def admit(self, key: Any = None) -> None:
        # Test-only forced admission; no backend write is needed when limits are bypassed.
        return None

    def clear(self) -> None:
        import re

        pattern = re.sub(r"([\\*?\[\]])", r"\\\1", self.key + ":") + "*"
        for key in self.client.scan_iter(match=pattern, count=200):
            self.client.delete(key)


def shared_rate_limit(backend: RedisWindow, *, block: bool = False) -> Callable[[F], F]:
    """Apply a shared RedisWindow to sync/async functions, respecting test overrides."""

    def decorate(func: F) -> F:
        if _core.is_async(func):

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                while not _core.overrides.disable_rate_limits:
                    wait = await asyncio.to_thread(backend.acquire)
                    if wait <= 0:
                        break
                    if not block:
                        raise RateLimitExceeded("shared rate limit exceeded", retry_after=wait)
                    await asyncio.sleep(wait)
                return await func(*args, **kw)

            return cast(F, aw)

        @functools.wraps(func)
        def sw(*args: Any, **kw: Any) -> Any:
            import time

            while not _core.overrides.disable_rate_limits:
                wait = backend.acquire()
                if wait <= 0:
                    break
                if not block:
                    raise RateLimitExceeded("shared rate limit exceeded", retry_after=wait)
                time.sleep(wait)
            return func(*args, **kw)

        return cast(F, sw)

    return decorate
