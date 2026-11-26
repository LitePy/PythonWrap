"""Redis coordination backends for circuit breakers and bulkheads."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import math
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from python_wrap import _core, _fork
from python_wrap._backend import BackendMonitor
from python_wrap._runtime import digest, seconds
from python_wrap.coordination import AlreadyRunning, Lease
from python_wrap.errors import BulkheadFull

__all__ = [
    "AsyncRedisBreakerState",
    "RedisBreakerState",
    "RedisRetryBudget",
    "SharedBackends",
    "SharedBulkhead",
]

_BREAKER = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local op=ARGV[1]; local threshold=tonumber(ARGV[2]); local window=tonumber(ARGV[3])
local reset=tonumber(ARGV[4]); local halfmax=tonumber(ARGV[5]); local target=tonumber(ARGV[6])
local n=tonumber(ARGV[10] or '1'); local frate=tonumber(ARGV[11] or '0')
local srate=tonumber(ARGV[12] or '0'); local minc=tonumber(ARGV[13] or '1')
local slow=tonumber(ARGV[14] or '0'); local rated=frate>0 or srate>0
local gen=tonumber(redis.call('HGET',KEYS[1],'gen') or '0')
local state=redis.call('HGET',KEYS[1],'state') or 'closed'; local old=state
local opened=tonumber(redis.call('HGET',KEYS[1],'opened') or '0')
local failures=tonumber(redis.call('HGET',KEYS[1],'failures') or '0')
local trials=tonumber(redis.call('HGET',KEYS[1],'trials') or '0')
local successes=tonumber(redis.call('HGET',KEYS[1],'successes') or '0')
local admitted=1; local trial=0; local wait=0; local trip=false
local wc,wf,ws=0,0,0
-- Calls, failures and slow calls in window, in ten slots of one hash (KEYS[3]).
local function slots(ac,af,as)
 local width=math.max(1,math.floor(window/10)); local epoch=math.floor(now/width)
 if ac>0 then redis.call('HINCRBY',KEYS[3],'c:'..epoch,ac)
  if af>0 then redis.call('HINCRBY',KEYS[3],'f:'..epoch,af) end
  if as>0 then redis.call('HINCRBY',KEYS[3],'s:'..epoch,as) end
  redis.call('PEXPIRE',KEYS[3],window*2) end
 local c,f,s=0,0,0
 local all=redis.call('HGETALL',KEYS[3])
 for i=1,#all,2 do
  local field=all[i]; local e=tonumber(string.sub(field,3))
  if e<=epoch-10 then redis.call('HDEL',KEYS[3],field)
  else local v=tonumber(all[i+1]); local k=string.sub(field,1,1)
   if k=='c' then c=c+v elseif k=='f' then f=f+v else s=s+v end end
 end
 return c,f,s
end
local function trips()
 if wc<minc then return false end
 return (frate>0 and wf>=frate*wc) or (srate>0 and ws>=srate*wc)
end
if op=='reset' then
 state='closed'; failures=0; trials=0; successes=0; opened=0; gen=gen+1
 redis.call('DEL',KEYS[2],KEYS[3])
 redis.call('HSET',KEYS[1],'total_failures',0,'total_successes',0,'total_rejected',0)
elseif op=='force_open' then state='open'; opened=now; trials=0; gen=gen+1
else
 if state=='open' and now-opened>=reset then
  state='half_open'; gen=gen+1; trials=0; successes=0
  redis.call('HSET',KEYS[1],'trial_deadline',now+math.max(1000,reset))
 end
 if op=='admit' then
  if state=='half_open' then
   local deadline=tonumber(redis.call('HGET',KEYS[1],'trial_deadline') or '0')
   if now>=deadline then gen=gen+1; trials=0; successes=0
    redis.call('HSET',KEYS[1],'trial_deadline',now+math.max(1000,reset)) end
  end
  if state=='open' or (state=='half_open' and trials>=halfmax) then
   admitted=0; wait=math.max(0,opened+reset-now)
   redis.call('HINCRBY',KEYS[1],'total_rejected',1)
  elseif state=='half_open' then trial=1; trials=trials+1 end
 elseif (op=='success' or op=='failure' or op=='neutral') and tonumber(ARGV[7])==gen then
  local is_trial=tonumber(ARGV[8])==1
  if is_trial then trials=math.max(0,trials-1) end
  if op=='success' then
   redis.call('HINCRBY',KEYS[1],'total_successes',n)
   if state=='half_open' then
    if slow==1 and srate>0 then trip=true
    else
     successes=successes+n
     if successes>=target then state='closed'; gen=gen+1; failures=0
      redis.call('DEL',KEYS[2],KEYS[3]) end
    end
   else
    if window==0 then failures=0 end
    if rated and not is_trial then wc,wf,ws=slots(n,0,slow*n); trip=trips() end
   end
  elseif op=='failure' then
   redis.call('HINCRBY',KEYS[1],'total_failures',1)
   if rated and not is_trial and state~='half_open' then
    wc,wf,ws=slots(1,1,slow); trip=trips()
   end
   if frate>0 then failures=wf
   elseif window>0 then
    redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now-window)
    redis.call('ZADD',KEYS[2],now,ARGV[9]); failures=redis.call('ZCARD',KEYS[2])
    redis.call('PEXPIRE',KEYS[2],window)
   else failures=failures+1 end
   if state=='half_open' or (frate==0 and failures>=threshold) then trip=true end
  end
  if trip then state='open'; opened=now; trials=0; successes=0; gen=gen+1 end
 end
end
if rated and wc==0 then wc,wf,ws=slots(0,0,0) end
redis.call('HSET',KEYS[1],'state',state,'gen',gen,'opened',opened,'failures',failures,
           'trials',trials,'successes',successes)
return {admitted,state,gen,trial,wait,failures,
 redis.call('HGET',KEYS[1],'total_failures') or '0',
 redis.call('HGET',KEYS[1],'total_successes') or '0',
 redis.call('HGET',KEYS[1],'total_rejected') or '0',opened,old,wc,wf,ws}
"""


_BREAKER_SHA = hashlib.sha1(_BREAKER.encode()).hexdigest()


def _no_script(exc: Exception) -> bool:
    """True for Redis's NOSCRIPT reply (redis-py: NoScriptError, "No matching script")."""
    text = str(exc)
    return type(exc).__name__ == "NoScriptError" or "NOSCRIPT" in text or "No matching" in text


_BUDGET = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local width=tonumber(ARGV[2]); local epoch=math.floor(now/width)
local op=ARGV[1]
if op=='deposit' then
 redis.call('HINCRBY',KEYS[1],'r:'..epoch,1); redis.call('PEXPIRE',KEYS[1],width*12); return 1
end
local req,ret=0,0
local all=redis.call('HGETALL',KEYS[1])
for i=1,#all,2 do
 local f=all[i]; local e=tonumber(string.sub(f,3))
 if e<=epoch-10 then redis.call('HDEL',KEYS[1],f)
 elseif string.sub(f,1,1)=='r' then req=req+tonumber(all[i+1])
 else ret=ret+tonumber(all[i+1]) end
end
if op=='totals' then return {req,ret} end
if tonumber(ARGV[3])*req+tonumber(ARGV[4])-ret>=1 then
 redis.call('HINCRBY',KEYS[1],'t:'..epoch,1); redis.call('PEXPIRE',KEYS[1],width*12); return 1
end
return 0
"""


class RedisRetryBudget:
    """A `RetryBudget` backend that counts requests and retries in Redis.

    Two counters per time slot live in one hash and one Lua script updates them on the
    Redis clock, so every process (and host) draws on one allowance. Keys come from
    ``name``: two services that use the same name share a budget on purpose. Pass it as
    ``RetryBudget(..., backend=RedisRetryBudget(client, "vendor"))``.
    """

    def __init__(
        self,
        client: Any,
        name: str,
        *,
        prefix: str = "python-wrap:budget:",
        on_backend_error: str | None = None,
        retry_backend_after: float = 1.0,
    ) -> None:
        if not name:
            raise ValueError("a shared retry budget needs a name")
        self.client, self.name = client, name
        self.key = prefix + "{" + digest(name) + "}"
        self._monitor = BackendMonitor("budget", name, on_backend_error, retry_backend_after)

    def _eval(self, budget: Any, op: str) -> Any:
        width = max(1, math.ceil(budget.window * 1000 / 10))
        return self.client.eval(_BUDGET, 1, self.key, op, width, budget.ratio, budget._reserve)

    def deposit(self, budget: Any) -> None:
        self._monitor.call("deposit", lambda: self._eval(budget, "deposit"), budget._deposit_local)

    def try_withdraw(self, budget: Any) -> bool:
        def fallback() -> bool:
            return budget._withdraw_local() if self._monitor.mode == "local" else True

        return bool(
            self._monitor.call(
                "withdraw", lambda: int(self._eval(budget, "withdraw")) == 1, fallback
            )
        )

    def totals(self, budget: Any) -> tuple[int, int]:
        def local() -> tuple[int, int]:
            with budget._lock:
                totals: tuple[int, int] = budget._totals(budget._slot()[1])
                return totals

        row = self._monitor.call("totals", lambda: self._eval(budget, "totals"), local)
        return int(row[0]), int(row[1])


class RedisBreakerState:
    """Atomic Redis breaker state, window failures and bounded half-open trial leases.

    Use the same name and settings in every process. Keys share a Redis Cluster hash
    tag. Backend errors reach the breaker, which applies its ``on_backend_error``; they
    are never read as a healthy dependency. Half-open reservations expire after
    recovery_timeout (minimum one second), so a crashed probing worker cannot
    permanently consume the trial slot.

    A closed state read from the store is trusted for ``state_ttl`` seconds: during that
    time calls are admitted without a round trip, successes are counted locally and sent
    in one batch every ``flush_interval`` seconds or ``flush_every`` calls, and the
    breaker's ``state``/``stats`` come from the cached answer. Failures, half-open trials
    and admin calls always go to the store. The trade: a process may keep admitting calls
    for up to ``state_ttl`` after another process opened the breaker. ``state_ttl=0``
    sends every call to the store. The script runs with ``EVALSHA`` and is loaded again
    on ``NOSCRIPT``.
    """

    def __init__(
        self,
        client: Any,
        prefix: str = "python-wrap:breaker:",
        *,
        state_ttl: float = 1.0,
        flush_interval: float = 1.0,
        flush_every: int = 1000,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must not be empty")
        if not math.isfinite(state_ttl) or state_ttl < 0:
            raise ValueError("state_ttl must be >= 0")
        if not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError("flush_interval must be >= 0")
        if isinstance(flush_every, bool) or not isinstance(flush_every, int) or flush_every < 1:
            raise ValueError("flush_every must be a positive integer")
        self.client, self.prefix = client, prefix
        self.state_ttl, self.flush_interval, self.flush_every = (
            state_ttl,
            flush_interval,
            flush_every,
        )
        # breaker name -> [last closed answer, read at, pending successes, last flush]
        self._cache: dict[str, list[Any]] = {}
        self._lock = _fork.ForkSafeLock()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._cache.clear()

    def _local(
        self, breaker: Any, operation: str, trial: bool, slow: bool = False
    ) -> tuple[Any, int, int]:
        """``(answer or None, successes to flush, their generation)`` from the cache."""
        if self.state_ttl == 0 or trial or slow:  # a slow call can open a rated breaker
            return None, 0, 0
        now = _core.monotonic()
        with self._lock:
            entry = self._cache.get(breaker.name)
            if entry is None:
                return None, 0, 0
            data, read_at, pending, flushed = entry
            generation = data["generation"]
            if operation in {"failure", "reset", "force_open"} or now - read_at >= self.state_ttl:
                # The store answers; a failure (or a refresh) carries batched successes first.
                entry[2], entry[3] = 0, now
                return None, 0 if operation in {"reset", "force_open"} else pending, generation
            if operation == "success":
                pending += 1
                if pending >= self.flush_every or now - flushed >= self.flush_interval:
                    entry[2], entry[3] = 0, now
                    return dict(data, old=data["state"]), pending, generation
                entry[2] = pending
            answer = dict(data, admitted=1, trial=0, wait=0, old=data["state"])
            answer["total_successes"] = data["total_successes"] + entry[2]
            return answer, 0, 0

    def _remember(self, breaker: Any, data: dict[str, Any]) -> dict[str, Any]:
        if self.state_ttl:
            with self._lock:
                if data["state"] == "closed":
                    old = self._cache.get(breaker.name)
                    pending, flushed = (old[2], old[3]) if old else (0, _core.monotonic())
                    self._cache[breaker.name] = [data, _core.monotonic(), pending, flushed]
                else:
                    self._cache.pop(breaker.name, None)
        return data

    def _call(self, args: tuple[Any, ...]) -> Any:
        evalsha = getattr(self.client, "evalsha", None)
        if evalsha is None:
            return self.client.eval(_BREAKER, *args)
        try:
            return evalsha(_BREAKER_SHA, *args)
        except Exception as exc:
            if not _no_script(exc):
                raise
            return self.client.eval(_BREAKER, *args)

    def operate(
        self,
        breaker: Any,
        operation: str,
        generation: int = 0,
        trial: bool = False,
        slow: bool = False,
    ) -> dict[str, Any]:
        data, flush, flush_gen = self._local(breaker, operation, trial, slow)
        if flush:
            self._remember(
                breaker,
                self._parse(self._call(self._args(breaker, "success", flush_gen, False, flush))),
            )
        if data is not None:
            return data  # type: ignore[no-any-return]
        row = self._call(self._args(breaker, operation, generation, trial, 1, slow))
        return self._remember(breaker, self._parse(row))

    def _args(
        self,
        breaker: Any,
        operation: str,
        generation: int,
        trial: bool,
        count: int = 1,
        slow: bool = False,
    ) -> tuple[Any, ...]:
        key = self.prefix + "{" + digest(breaker.name) + "}"
        return (
            3,
            key,
            key + ":failures",
            key + ":slots",
            operation,
            breaker.failure_threshold,
            0 if breaker.window is None else max(1, math.ceil(breaker.window * 1000)),
            math.ceil(breaker.recovery_timeout * 1000),
            breaker.half_open_max_calls,
            breaker.success_threshold,
            generation,
            int(trial),
            uuid.uuid4().hex,
            count,
            getattr(breaker, "failure_rate", None) or 0,
            getattr(breaker, "slow_call_rate", None) or 0,
            getattr(breaker, "minimum_calls", 1),
            int(slow),
        )

    @staticmethod
    def _parse(row: Any) -> dict[str, Any]:
        keys = (
            "admitted",
            "state",
            "generation",
            "trial",
            "wait",
            "failures",
            "total_failures",
            "total_successes",
            "total_rejected",
            "opened",
            "old",
            "window_calls",
            "window_failures",
            "window_slow",
        )
        row = [*row, *[0] * (len(keys) - len(row))]  # scripts before 0.4.0b2 sent 11 values
        values = [value.decode() if isinstance(value, bytes) else value for value in row]
        data = dict(zip(keys, values, strict=True))
        for name in keys:
            if name not in {"state", "old"}:
                data[name] = int(data[name])
        return data


class AsyncRedisBreakerState(RedisBreakerState):
    """`RedisBreakerState` for ``redis.asyncio`` clients: ``operate`` is awaited.

    Same Lua script and keys, so sync and async processes share one breaker. A breaker
    with this backend protects ``async def`` functions only; its sync ``state`` and
    ``stats`` report the last state the store returned in this process.
    """

    async def _acall(self, args: tuple[Any, ...]) -> Any:
        evalsha = getattr(self.client, "evalsha", None)
        if evalsha is None:
            return await self.client.eval(_BREAKER, *args)
        try:
            return await evalsha(_BREAKER_SHA, *args)
        except Exception as exc:
            if not _no_script(exc):
                raise
            return await self.client.eval(_BREAKER, *args)

    async def operate(  # type: ignore[override]
        self,
        breaker: Any,
        operation: str,
        generation: int = 0,
        trial: bool = False,
        slow: bool = False,
    ) -> dict[str, Any]:
        data, flush, flush_gen = self._local(breaker, operation, trial, slow)
        if flush:
            row = await self._acall(self._args(breaker, "success", flush_gen, False, flush))
            self._remember(breaker, self._parse(row))
        if data is not None:
            return data  # type: ignore[no-any-return]
        row = await self._acall(self._args(breaker, operation, generation, trial, 1, slow))
        return self._remember(breaker, self._parse(row))


class SharedBulkhead:
    """Renewable lease-backed concurrency cap; max_waiting is a per-process queue bound."""

    def __init__(
        self,
        max_concurrent: int,
        *,
        backend: Any,
        name: str,
        max_waiting: int | None = None,
        timeout: float | None = None,
        ttl: float = 60,
        on_backend_error: str | None = None,
        retry_backend_after: float = 1.0,
    ) -> None:
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or max_concurrent < 1
        ):
            raise ValueError("max_concurrent must be a positive integer")
        if max_waiting is not None and (
            isinstance(max_waiting, bool) or not isinstance(max_waiting, int) or max_waiting < 0
        ):
            raise ValueError("max_waiting must be a nonnegative integer")
        if not name:
            raise ValueError("shared bulkheads require an explicit name")
        self.capacity, self.backend, self.name = max_concurrent, backend, name
        self.max_waiting = max_waiting
        self.timeout = None if timeout is None else seconds(timeout, positive=False)
        self.ttl = seconds(ttl)
        self._monitor = BackendMonitor("bulkhead", name, on_backend_error, retry_backend_after)
        acquire = getattr(backend, "acquire", None)
        self._async = acquire is not None and _core.is_async(acquire)
        self._stack: contextvars.ContextVar[tuple[tuple[Lease, object], ...]] = (
            contextvars.ContextVar("bulkhead_leases", default=())
        )
        self._lock = _fork.ForkSafeLock()
        self._waiting = self._active = 0
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._waiting = self._active = 0
        self._stack.set(())

    @property
    def waiting(self) -> int:
        with self._lock:
            return self._waiting

    @property
    def active(self) -> int:
        """Active calls in this process (the backend enforces the global limit)."""
        with self._lock:
            return self._active

    def _store_down(self, exc: Exception) -> None:
        """Apply on_backend_error to a failed lease call (re-raise in "raise" mode)."""
        self._monitor.failed(exc, "acquire")
        if self._monitor.mode == "raise":
            raise exc

    def _local_admit(self, owner: object) -> None:
        """Store is down: "allow" admits; "local" admits up to this process's capacity."""
        if self._monitor.mode == "local" and self.active >= self.capacity:
            raise self._rejected("shared bulkhead full (local fallback)")
        self._admitted(None, owner)  # type: ignore[arg-type]

    def _lease(self) -> Lease:
        return Lease(self.name, backend=self.backend, ttl=self.ttl, limit=self.capacity, wait=False)

    def _rejected(self, reason: str) -> BulkheadFull:
        _core.emit("bulkhead.rejected", self.name, active=self.active, waiting=self.waiting)
        return BulkheadFull(reason, name=self.name, max_concurrent=self.capacity)

    def _queued(self) -> None:
        with self._lock:
            rejected = self.max_waiting is not None and self._waiting >= self.max_waiting
            if not rejected:
                self._waiting += 1
        if rejected:
            raise self._rejected("shared bulkhead queue full")

    def _admitted(self, lease: Lease, owner: object) -> None:
        self._stack.set((*self._stack.get(), (lease, owner)))
        with self._lock:
            self._active += 1

    def acquire(self) -> None:
        if self._async:
            raise TypeError(
                "this bulkhead's backend is async; it protects async def functions only"
            )
        if self._monitor._skipping():
            return self._local_admit(threading.get_ident())
        lease = self._lease()
        try:
            lease.__enter__()
        except AlreadyRunning:
            self._queued()
            try:
                lease.wait, lease.timeout = True, self.timeout
                lease.__enter__()
            except AlreadyRunning as exc:
                raise self._rejected("shared bulkhead timed out") from exc
            finally:
                with self._lock:
                    self._waiting -= 1
        except Exception as exc:
            self._store_down(exc)
            return self._local_admit(threading.get_ident())
        self._monitor.succeeded()
        self._admitted(lease, threading.get_ident())

    def release(self, typ: Any = None, exc: Any = None, tb: Any = None) -> None:
        stack = self._stack.get()
        if not stack:
            raise RuntimeError("shared bulkhead released without acquisition")
        lease, owner = stack[-1]
        if owner != threading.get_ident():
            raise RuntimeError("shared bulkhead must be released in its acquiring thread")
        self._stack.set(stack[:-1])
        try:
            if lease is not None:
                lease.__exit__(typ, exc, tb)
        finally:
            with self._lock:
                self._active -= 1

    async def acquire_async(self) -> None:
        if self._monitor._skipping():
            return self._local_admit(asyncio.current_task())
        lease = self._lease()
        try:
            await lease.__aenter__()
        except AlreadyRunning:
            self._queued()
            try:
                lease.wait, lease.timeout = True, self.timeout
                await lease.__aenter__()
            except AlreadyRunning as exc:
                raise self._rejected("shared bulkhead timed out") from exc
            finally:
                with self._lock:
                    self._waiting -= 1
        except Exception as exc:
            self._store_down(exc)
            return self._local_admit(asyncio.current_task())
        self._monitor.succeeded()
        self._admitted(lease, asyncio.current_task())

    async def release_async(self, typ: Any = None, exc: Any = None, tb: Any = None) -> None:
        stack = self._stack.get()
        if not stack:
            raise RuntimeError("shared bulkhead released without acquisition")
        lease, owner = stack[-1]
        if owner is not asyncio.current_task():
            raise RuntimeError("shared bulkhead must be released in its acquiring task")
        self._stack.set(stack[:-1])
        try:
            if lease is not None:
                await lease.__aexit__(typ, exc, tb)
        finally:
            with self._lock:
                self._active -= 1


@dataclass
class SharedBackends:
    """The shared stores a preset or policy uses, built from one Redis client.

    Give a bare ``client`` to share everything, or set a field to override one layer:
    ``breaker_state`` (a `RedisBreakerState`), ``window`` (a Redis client for rate
    windows), ``semaphore`` (a lease backend such as `RedisSemaphore`) and ``budget``
    (a `RedisRetryBudget`-style backend). Keys derive from the preset or policy name.
    """

    client: Any = None
    breaker_state: Any = None
    window: Any = None
    semaphore: Any = None
    budget: Any = None

    @property
    def is_async(self) -> bool:
        """True for a ``redis.asyncio`` client: layers are built from the async classes."""
        return self.client is not None and _core.is_async(self.client.eval)

    def breaker(self) -> Any:
        if self.breaker_state is not None:
            return self.breaker_state
        if self.client is None:
            return None
        return (AsyncRedisBreakerState if self.is_async else RedisBreakerState)(self.client)

    def rate_window(self, name: str, calls: int, period: float) -> Any:
        from python_wrap.quotas import AsyncRedisWindow, RedisWindow

        client = self.window if self.window is not None else self.client
        if client is None:
            return None
        kind = AsyncRedisWindow if _core.is_async(client.eval) else RedisWindow
        return kind(client, f"python-wrap:window:{name}", calls=calls, period=period)

    def lease_backend(self) -> Any:
        if self.semaphore is not None:
            return self.semaphore
        if self.client is None:
            return None
        from python_wrap.coordination import AsyncRedisSemaphore, RedisSemaphore

        return (AsyncRedisSemaphore if self.is_async else RedisSemaphore)(self.client)

    def budget_backend(self, name: str) -> Any:
        if self.budget is not None:
            return self.budget
        # shortcut: retry budgets are sync only, so an async client keeps them per process;
        # pass budget=RedisRetryBudget(sync_client, name) to share them.
        if self.client is None or self.is_async:
            return None
        return RedisRetryBudget(self.client, name)


def resolve_shared(shared: Any) -> SharedBackends | None:
    """Turn ``shared=`` (a client or `SharedBackends`) or ``configure(shared=)`` into one."""
    if shared is None:
        from python_wrap.config import get_config

        shared = get_config()["shared"]
    if shared is None or shared is False:
        return None
    return shared if isinstance(shared, SharedBackends) else SharedBackends(client=shared)
