"""Token limits and budgets shared by several worker processes.

A `TokenRateLimiter` keeps its bucket in memory, so ten workers each believe they own
the whole tokens-per-minute quota and together trigger a 429 storm. The windows here move
the bucket into storage every worker can reach, so the whole fleet draws from one bucket
and backs off together:

- `SQLiteTokenWindow`: one database file (WAL mode, ``BEGIN IMMEDIATE`` transactions);
  works across processes on one host with no server.
- `RedisTokenWindow` / `AsyncRedisTokenWindow`: one hash updated by a Lua script that
  reads the Redis server's clock, so hosts with skewed clocks still agree.
- `SQLiteBudgetBackend`: a `python_wrap.llm.BudgetBackend` on SQLite, so a `Budget`
  can be shared the same way.

Plug a window into ``token_rate_limit(..., store=window)``. `observe_headers` (also a
method of the windows and of the limiter) feeds the provider's own rate-limit headers
(``x-ratelimit-remaining-tokens``, ``anthropic-ratelimit-*``, ``retry-after``) back into
the window, so one worker's 429 makes every worker wait.

Nothing here imports redis or a provider SDK. Clocks come from ``python_wrap._core``, so
`python_wrap.testing.fake_clock` controls them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from python_wrap import _core

__all__ = [
    "AsyncRedisTokenWindow",
    "RateLimitHint",
    "RedisTokenWindow",
    "SQLiteBudgetBackend",
    "SQLiteTokenWindow",
    "SharedTokenWindow",
    "parse_rate_limit_headers",
]

# --------------------------------------------------------------------- header parsing

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_REMAINING = (
    "x-ratelimit-remaining-tokens",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-output-tokens-remaining",
)
_RESET = (
    "x-ratelimit-reset-tokens",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-reset",
)


@dataclasses.dataclass(frozen=True)
class RateLimitHint:
    """What a provider's rate-limit headers said, in plain numbers.

    Attributes:
        remaining_tokens: Tokens the provider says are left in the window (the smallest
            figure when input and output limits are reported separately), or None.
        reset_seconds: Seconds until the window refills (the latest figure), or None.
        retry_after: Seconds from ``Retry-After`` / ``retry-after-ms``, or None.
    """

    remaining_tokens: float | None = None
    reset_seconds: float | None = None
    retry_after: float | None = None

    @property
    def block_seconds(self) -> float | None:
        """How long to stop sending: ``retry_after``, else ``reset_seconds`` at zero tokens."""
        if self.retry_after is not None:
            return self.retry_after
        if self.remaining_tokens is not None and self.remaining_tokens <= 0:
            return self.reset_seconds
        return None


def _lookup(headers: Any, name: str) -> str | None:
    """Case-insensitive header read from a mapping, pair list or httpx-like object."""
    if headers is None:
        return None
    try:
        items: Iterable[Any] = headers.items() if hasattr(headers, "items") else headers
        for key, value in items:
            if isinstance(key, bytes):
                key = key.decode("latin-1")
            if isinstance(key, str) and key.lower() == name:
                if isinstance(value, bytes):
                    value = value.decode("latin-1")
                return str(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _timestamp(text: str) -> float | None:
    """Seconds from now until an RFC 3339 or HTTP date (never negative)."""
    moment: datetime | None = None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(moment.timestamp() - _core.wall_time(), 0.0)


def _seconds(text: str | None) -> float | None:
    """A duration (``"6m0s"``, ``"20ms"``, ``"1.5"``) or a date as seconds from now."""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        pass
    else:
        return max(number, 0.0) if math.isfinite(number) else None
    pos, total = 0, 0.0
    while pos < len(value):
        found = _DURATION.match(value, pos)
        if found is None:
            return _timestamp(value)
        total += float(found.group(1)) * _UNITS[found.group(2)]
        pos = found.end()
    return total


def _count(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        number = float(text.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_rate_limit_headers(headers: Any) -> RateLimitHint:
    """Read token-window hints from provider response headers; never raises.

    Understands the OpenAI style (``x-ratelimit-remaining-tokens``,
    ``x-ratelimit-reset-tokens`` as ``"6m0s"`` / ``"20ms"``), the Anthropic style
    (``anthropic-ratelimit-tokens-remaining`` / ``-reset`` as an RFC 3339 time, plus the
    separate ``input-tokens`` / ``output-tokens`` variants) and ``Retry-After`` (seconds
    or HTTP date) or ``retry-after-ms``. Header names are case-insensitive. Unparseable
    values are ignored.

    Args:
        headers: A mapping, a list of ``(name, value)`` pairs, or any object with
            ``items()`` (such as ``httpx.Headers``).

    Example:
        >>> hint = parse_rate_limit_headers(
        ...     {"X-RateLimit-Remaining-Tokens": "0", "X-RateLimit-Reset-Tokens": "1m30s"}
        ... )
        >>> hint.remaining_tokens, hint.reset_seconds, hint.block_seconds
        (0.0, 90.0, 90.0)
    """
    counts = [c for c in (_count(_lookup(headers, n)) for n in _REMAINING) if c is not None]
    resets = [r for r in (_seconds(_lookup(headers, n)) for n in _RESET) if r is not None]
    retry = _seconds(_lookup(headers, "retry-after"))
    millis = _count(_lookup(headers, "retry-after-ms"))
    if millis is not None and millis >= 0:
        retry = millis / 1000.0 if retry is None else max(retry, millis / 1000.0)
    return RateLimitHint(
        remaining_tokens=min(counts) if counts else None,
        reset_seconds=max(resets) if resets else None,
        retry_after=retry,
    )


# ------------------------------------------------------------------- the bucket maths

_State = tuple[float | None, float, float]  # level (None = unknown), stamp, blocked-until


def _step(
    state: _State | None,
    op: str,
    a: float,
    b: float,
    capacity: float,
    rate: float,
    now: float,
) -> tuple[_State, float]:
    """One atomic bucket operation; the Lua script below mirrors it line for line.

    ``capacity`` 0 means "unknown" (an observation from a worker that does not know the
    limit): the level is then left as it is. Ops: ``reserve`` (``a`` tokens; returns the
    wait, 0 when admitted), ``force`` (``a`` tokens), ``settle`` (``a`` reserved, ``b``
    actual), ``level`` and ``observe`` (``a`` remaining or -1, ``b`` block seconds or -1).
    """
    level, stamp, blocked = (None, now, 0.0) if state is None else state
    if capacity > 0:
        grown = capacity if level is None else level + max(now - stamp, 0.0) * rate
        level = min(capacity, grown)
        stamp = now
    result = 0.0
    if op == "reserve":
        assert level is not None
        if blocked > now:
            result = blocked - now
        else:
            need = min(a, capacity)
            if level + 1e-9 >= need:
                level -= a
            else:
                result = (need - level) / rate
    elif op == "force":
        assert level is not None
        level -= a
    elif op == "settle":
        assert level is not None
        level = min(capacity, level + a - b)
    elif op == "level":
        assert level is not None
        result = level
    elif op == "observe":
        if a >= 0:
            level = a if level is None else min(level, a)
            stamp = now
        if b > 0:
            blocked = max(blocked, now + b)
        result = max(blocked - now, 0.0)
    else:
        raise ValueError(f"unknown window operation {op!r}")
    return (level, stamp, blocked), result


_LUA_STEP = """
local t=redis.call('TIME')
local now=tonumber(t[1])+tonumber(t[2])/1000000
local h=redis.call('HMGET',KEYS[1],'level','stamp','blocked')
local op=ARGV[1]
local a=tonumber(ARGV[2])
local b=tonumber(ARGV[3])
local cap=tonumber(ARGV[4])
local rate=tonumber(ARGV[5])
local level=tonumber(h[1])
local stamp=tonumber(h[2]) or now
local blocked=tonumber(h[3]) or 0
if cap>0 then
 if level==nil then level=cap else level=math.min(cap, level+math.max(now-stamp,0)*rate) end
 stamp=now
end
local result=0
if op=='reserve' then
 if blocked>now then result=blocked-now
 else
  local need=math.min(a,cap)
  if level+1e-9>=need then level=level-a else result=(need-level)/rate end
 end
elseif op=='force' then level=level-a
elseif op=='settle' then level=math.min(cap, level+a-b)
elseif op=='level' then result=level
elseif op=='observe' then
 if a>=0 then
  if level==nil then level=a else level=math.min(level,a) end
  stamp=now
 end
 if b>0 then blocked=math.max(blocked, now+b) end
 result=math.max(blocked-now,0)
end
if level~=nil then
 redis.call('HSET',KEYS[1],'level',string.format('%.9f',level),'stamp',string.format('%.6f',stamp),'blocked',string.format('%.6f',blocked))
else
 redis.call('HSET',KEYS[1],'stamp',string.format('%.6f',stamp),'blocked',string.format('%.6f',blocked))
end
local ttl=math.ceil((cap/math.max(rate,1e-9)+math.max(blocked-now,0))*1000)+60000
redis.call('PEXPIRE',KEYS[1],ttl)
return string.format('%.9f',result)
"""


# --------------------------------------------------------------------------- the base


class SharedTokenWindow:
    """A token bucket held in shared storage; the base of the concrete windows.

    A limiter calls `reserve`, `force`, `settle` and `level` with its own capacity and
    refill rate, so every process using one window must agree on those numbers. Time is
    wall-clock (via ``python_wrap._core.wall_time``) because processes do not share a
    monotonic clock. A subclass implements ``_transact``.

    When the storage fails (locked database, unreachable Redis, an exception from the
    client) and ``fail_open`` is true, the window logs a ``rate_limit.store_error`` event
    and falls back to a private in-memory bucket for that call, so an outage degrades to
    per-process limiting instead of stopping every LLM call. ``fail_open=False`` raises.

    Attributes:
        name: Identifies the bucket inside the storage; share it to share the quota.
        fail_open: Whether storage errors fall back to a local bucket.
    """

    def __init__(self, name: str = "tokens", *, fail_open: bool = True) -> None:
        if not name:
            raise ValueError("a window needs a nonempty name")
        self.name = name
        self.fail_open = fail_open
        self._fallback: _State | None = None
        self._flock = threading.Lock()

    # -- storage hook ------------------------------------------------------------
    def _transact(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        raise NotImplementedError

    def _local(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        with self._flock:
            self._fallback, result = _step(
                self._fallback, op, a, b, capacity, rate, _core.wall_time()
            )
        return result

    def _run(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        try:
            return self._transact(op, a, b, capacity, rate)
        except Exception as exc:
            _core.emit("rate_limit.store_error", self.name, error=type(exc).__name__, op=op)
            if not self.fail_open:
                raise
            return self._local(op, a, b, capacity, rate)

    # -- the limiter-facing operations --------------------------------------------
    def reserve(self, tokens: int, capacity: float, rate: float) -> float:
        """Reserve ``tokens``: 0.0 when admitted, else seconds to wait."""
        return self._run("reserve", float(tokens), 0.0, float(capacity), float(rate))

    def force(self, tokens: int, capacity: float, rate: float) -> None:
        """Take ``tokens`` without checking (goes into debt)."""
        self._run("force", float(tokens), 0.0, float(capacity), float(rate))

    def settle(self, reserved: int, actual: int, capacity: float, rate: float) -> None:
        """Replace a reservation by the real usage."""
        self._run("settle", float(reserved), float(actual), float(capacity), float(rate))

    def level(self, capacity: float, rate: float) -> float:
        """Tokens available now (negative while in debt)."""
        return self._run("level", 0.0, 0.0, float(capacity), float(rate))

    def observe(
        self,
        *,
        remaining: float | None = None,
        block_for: float | None = None,
        capacity: float = 0.0,
        rate: float = 1.0,
    ) -> float:
        """Apply provider knowledge: cap the level at ``remaining`` and/or block for seconds.

        Returns the seconds the window is blocked for after the update (0 when open).
        """
        return self._run(
            "observe",
            -1.0 if remaining is None else max(float(remaining), 0.0),
            -1.0 if block_for is None else max(float(block_for), 0.0),
            float(capacity),
            float(rate),
        )

    def observe_headers(
        self, headers: Any, *, capacity: float = 0.0, rate: float = 1.0
    ) -> RateLimitHint:
        """Update the window from provider rate-limit headers; see `parse_rate_limit_headers`.

        ``remaining`` lowers the level (never raises it), and ``Retry-After`` (or the reset
        time when no tokens remain) blocks every worker until then. A limiter calls this
        with its own ``capacity`` and ``rate``; called alone, only blocking and the
        remaining count are applied.
        """
        hint = parse_rate_limit_headers(headers)
        block = hint.block_seconds
        if hint.remaining_tokens is not None or block is not None:
            self.observe(
                remaining=hint.remaining_tokens, block_for=block, capacity=capacity, rate=rate
            )
        return hint


# ---------------------------------------------------------------------------- SQLite

_QUARANTINE_ERRORS = ("not a database", "malformed", "disk image", "corrupt")


def _is_corrupt(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and any(m in text for m in _QUARANTINE_ERRORS)


class _SQLiteFile:
    """Connection handling shared by the SQLite window and budget backend."""

    def __init__(self, path: str | os.PathLike[str], timeout: float, schema: str) -> None:
        self.path = os.fspath(path)
        self.timeout = timeout
        self._schema = schema
        self._prepared = False
        self._plock = threading.Lock()

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """A fresh connection inside a ``BEGIN IMMEDIATE`` transaction (commit or rollback)."""
        self._prepare()
        conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _prepare(self) -> None:
        if self._prepared:
            return
        with self._plock:
            if self._prepared:
                return
            try:
                self._create()
            except sqlite3.DatabaseError as exc:
                if not _is_corrupt(exc):
                    raise
                self.quarantine()
                self._create()
            self._prepared = True

    def _create(self) -> None:
        conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self._schema)
        finally:
            conn.close()

    def quarantine(self) -> None:
        """Move an unreadable database aside (``<path>.corrupt``) so a fresh one is created."""
        for suffix in ("", "-wal", "-shm"):
            source = self.path + suffix
            if os.path.exists(source):
                with contextlib.suppress(OSError):
                    os.replace(source, self.path + ".corrupt" + suffix)
        self._prepared = False

    def guard(self, action: Any) -> Any:
        """Run ``action`` once more after quarantining a corrupt file."""
        try:
            return action()
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise
            with self._plock:
                self.quarantine()
            return action()


class SQLiteTokenWindow(SharedTokenWindow):
    """A `SharedTokenWindow` in one SQLite file, shared by processes on one host.

    Every operation is a ``BEGIN IMMEDIATE`` transaction on a short-lived connection
    (WAL journal), so it is atomic across processes, threads and ``fork``. A corrupt
    file is moved aside to ``<path>.corrupt`` and rebuilt. Several named windows can
    share one file.

    Args:
        path: The database file (created if missing).
        name: Bucket name inside the file.
        timeout: Seconds to wait for a competing writer before the storage counts as
            failed (see ``fail_open``).
        fail_open: Fall back to an in-memory bucket when storage fails.

    Example:
        ```python
        import os
        import tempfile

        from python_wrap.llm import token_rate_limit
        from python_wrap.llm_shared import SQLiteTokenWindow

        path = os.path.join(tempfile.mkdtemp(), "tokens.db")
        limit = token_rate_limit(1000, estimate=400, block=False, store=SQLiteTokenWindow(path))
        other_worker = SQLiteTokenWindow(path)  # another process would open the same file
        assert other_worker.level(1000, 1000 / 60) == 1000
        limit.try_reserve(400)
        assert other_worker.level(1000, 1000 / 60) < 700
        ```
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        name: str = "tokens",
        *,
        timeout: float = 10.0,
        fail_open: bool = True,
    ) -> None:
        super().__init__(name, fail_open=fail_open)
        self._file = _SQLiteFile(
            path,
            timeout,
            "CREATE TABLE IF NOT EXISTS windows ("
            "name TEXT PRIMARY KEY, level REAL, stamp REAL NOT NULL, blocked REAL NOT NULL)",
        )

    @property
    def path(self) -> str:
        """The database file."""
        return self._file.path

    def _once(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        now = _core.wall_time()
        with self._file.connection() as conn:
            row = conn.execute(
                "SELECT level, stamp, blocked FROM windows WHERE name=?", (self.name,)
            ).fetchone()
            state: _State | None = None if row is None else (row[0], row[1], row[2])
            new, result = _step(state, op, a, b, capacity, rate, now)
            conn.execute(
                "INSERT OR REPLACE INTO windows(name, level, stamp, blocked) VALUES (?,?,?,?)",
                (self.name, new[0], new[1], new[2]),
            )
        return float(result)

    def _transact(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        result: float = self._file.guard(lambda: self._once(op, a, b, capacity, rate))
        return result

    def clear(self) -> None:
        """Forget this bucket (it starts full again)."""
        with self._file.connection() as conn:
            conn.execute("DELETE FROM windows WHERE name=?", (self.name,))


class SQLiteBudgetBackend:
    """A `python_wrap.llm.BudgetBackend` in SQLite: one budget shared by local processes.

    ``add`` is one ``BEGIN IMMEDIATE`` transaction, so concurrent workers never lose
    spend; ``ttl`` expiry (windowed budgets) is honoured with the wall clock.

    Args:
        path: The database file (created if missing; a corrupt file is moved aside).
        timeout: Seconds to wait for a competing writer.

    Example:
        ```python
        import os
        import tempfile

        from python_wrap.llm import Budget
        from python_wrap.llm_shared import SQLiteBudgetBackend

        path = os.path.join(tempfile.mkdtemp(), "budget.db")
        budget = Budget(tokens=1000, backend=SQLiteBudgetBackend(path))
        budget.record({"input_tokens": 300, "output_tokens": 100})
        assert Budget(tokens=1000, backend=SQLiteBudgetBackend(path)).spent().tokens == 400
        ```
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 10.0) -> None:
        self._file = _SQLiteFile(
            path,
            timeout,
            "CREATE TABLE IF NOT EXISTS budgets ("
            "key TEXT PRIMARY KEY, tokens INTEGER NOT NULL, usd REAL NOT NULL, expires REAL)",
        )

    @property
    def path(self) -> str:
        """The database file."""
        return self._file.path

    def _add(self, key: str, tokens: int, usd: float, ttl: float | None) -> tuple[int, float]:
        now = _core.wall_time()
        with self._file.connection() as conn:
            row = conn.execute(
                "SELECT tokens, usd, expires FROM budgets WHERE key=?", (key,)
            ).fetchone()
            used_tokens, used_usd, expires = 0, 0.0, None
            if row is not None and (row[2] is None or now < row[2]):
                used_tokens, used_usd, expires = row
            if expires is None and ttl is not None:
                expires = now + ttl
            total = (int(used_tokens) + tokens, float(used_usd) + usd)
            conn.execute(
                "INSERT OR REPLACE INTO budgets(key, tokens, usd, expires) VALUES (?,?,?,?)",
                (key, total[0], total[1], expires),
            )
        return total

    def add(self, key: str, tokens: int, usd: float, ttl: float | None, /) -> tuple[int, float]:
        """Add to a counter atomically and return its new ``(tokens, usd)``."""
        result: tuple[int, float] = self._file.guard(lambda: self._add(key, tokens, usd, ttl))
        return result

    def _get(self, key: str) -> tuple[int, float]:
        now = _core.wall_time()
        with self._file.connection() as conn:
            row = conn.execute(
                "SELECT tokens, usd, expires FROM budgets WHERE key=?", (key,)
            ).fetchone()
        if row is None or (row[2] is not None and now >= row[2]):
            return 0, 0.0
        return int(row[0]), float(row[1])

    def get(self, key: str, /) -> tuple[int, float]:
        """Return the counter's ``(tokens, usd)``; ``(0, 0.0)`` when unknown or expired."""
        result: tuple[int, float] = self._file.guard(lambda: self._get(key))
        return result

    def delete(self, key: str, /) -> None:
        """Forget a counter."""

        def action() -> None:
            with self._file.connection() as conn:
                conn.execute("DELETE FROM budgets WHERE key=?", (key,))

        self._file.guard(action)


# ----------------------------------------------------------------------------- Redis


def _redis_args(
    name: str, prefix: str, op: str, a: float, b: float, cap: float, rate: float
) -> Any:
    return (_LUA_STEP, 1, f"{prefix}:{name}", op, repr(a), repr(b), repr(cap), repr(rate))


class RedisTokenWindow(SharedTokenWindow):
    """A `SharedTokenWindow` on Redis, shared by every worker on every host.

    Takes a synchronous ``redis.Redis``-compatible client (only ``eval`` is used) and
    never imports redis. Each operation is one Lua script that reads the server's
    clock (``TIME``), so workers with skewed clocks agree, and keys expire on their own
    once the bucket would be full again. Async limiters run the round trip in a worker
    thread; use `AsyncRedisTokenWindow` with a ``redis.asyncio`` client to stay on the
    loop.

    Args:
        client: The Redis client.
        name: Bucket name; share it to share the quota.
        prefix: Namespace for the key.
        fail_open: Fall back to an in-memory bucket when Redis fails.
    """

    def __init__(
        self,
        client: Any,
        name: str = "tokens",
        *,
        prefix: str = "python-wrap:tokens",
        fail_open: bool = True,
    ) -> None:
        super().__init__(name, fail_open=fail_open)
        if not prefix:
            raise ValueError("RedisTokenWindow needs a nonempty prefix")
        self.client, self.prefix = client, prefix

    def _transact(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        reply = self.client.eval(*_redis_args(self.name, self.prefix, op, a, b, capacity, rate))
        return float(reply)


class AsyncRedisTokenWindow(SharedTokenWindow):
    """`RedisTokenWindow` for ``redis.asyncio`` clients: round trips are awaited.

    Same script and keys as `RedisTokenWindow`, so sync and async workers can share one
    bucket. The ``a``-prefixed methods (`areserve`, `aforce`, `asettle`, `alevel`,
    `aobserve`, `aobserve_headers`) are the native ones; a limiter built on this window
    must be used from async code.
    """

    def __init__(
        self,
        client: Any,
        name: str = "tokens",
        *,
        prefix: str = "python-wrap:tokens",
        fail_open: bool = True,
    ) -> None:
        super().__init__(name, fail_open=fail_open)
        if not prefix:
            raise ValueError("AsyncRedisTokenWindow needs a nonempty prefix")
        self.client, self.prefix = client, prefix

    def _transact(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        raise RuntimeError(
            "AsyncRedisTokenWindow is async-only: use token_rate_limit(...) on async functions"
        )

    async def _arun(self, op: str, a: float, b: float, capacity: float, rate: float) -> float:
        try:
            reply = await self.client.eval(
                *_redis_args(self.name, self.prefix, op, a, b, capacity, rate)
            )
            return float(reply)
        except Exception as exc:
            _core.emit("rate_limit.store_error", self.name, error=type(exc).__name__, op=op)
            if not self.fail_open:
                raise
            return self._local(op, a, b, capacity, rate)

    async def areserve(self, tokens: int, capacity: float, rate: float) -> float:
        """Async `reserve`."""
        return await self._arun("reserve", float(tokens), 0.0, float(capacity), float(rate))

    async def aforce(self, tokens: int, capacity: float, rate: float) -> None:
        """Async `force`."""
        await self._arun("force", float(tokens), 0.0, float(capacity), float(rate))

    async def asettle(self, reserved: int, actual: int, capacity: float, rate: float) -> None:
        """Async `settle`."""
        await self._arun("settle", float(reserved), float(actual), float(capacity), float(rate))

    async def alevel(self, capacity: float, rate: float) -> float:
        """Async `level`."""
        return await self._arun("level", 0.0, 0.0, float(capacity), float(rate))

    async def aobserve(
        self,
        *,
        remaining: float | None = None,
        block_for: float | None = None,
        capacity: float = 0.0,
        rate: float = 1.0,
    ) -> float:
        """Async `observe`."""
        return await self._arun(
            "observe",
            -1.0 if remaining is None else max(float(remaining), 0.0),
            -1.0 if block_for is None else max(float(block_for), 0.0),
            float(capacity),
            float(rate),
        )

    async def aobserve_headers(
        self, headers: Any, *, capacity: float = 0.0, rate: float = 1.0
    ) -> RateLimitHint:
        """Async `observe_headers`."""
        hint = parse_rate_limit_headers(headers)
        block = hint.block_seconds
        if hint.remaining_tokens is not None or block is not None:
            await self.aobserve(
                remaining=hint.remaining_tokens, block_for=block, capacity=capacity, rate=rate
            )
        return hint
