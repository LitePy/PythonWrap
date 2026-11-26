"""Redis coordination backends for circuit breakers and bulkheads."""

from __future__ import annotations

import asyncio
import contextvars
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
local gen=tonumber(redis.call('HGET',KEYS[1],'gen') or '0')
local state=redis.call('HGET',KEYS[1],'state') or 'closed'; local old=state
local opened=tonumber(redis.call('HGET',KEYS[1],'opened') or '0')
local failures=tonumber(redis.call('HGET',KEYS[1],'failures') or '0')
local trials=tonumber(redis.call('HGET',KEYS[1],'trials') or '0')
local successes=tonumber(redis.call('HGET',KEYS[1],'successes') or '0')
local admitted=1; local trial=0; local wait=0
if op=='reset' then
 state='closed'; failures=0; trials=0; successes=0; opened=0; gen=gen+1
 redis.call('DEL',KEYS[2])
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
  if tonumber(ARGV[8])==1 then trials=math.max(0,trials-1) end
  if op=='success' then
   redis.call('HINCRBY',KEYS[1],'total_successes',1)
   if state=='half_open' then
    successes=successes+1
    if successes>=target then state='closed'; gen=gen+1; failures=0; redis.call('DEL',KEYS[2]) end
   elseif window==0 then failures=0 end
  elseif op=='failure' then
   redis.call('HINCRBY',KEYS[1],'total_failures',1)
   if window>0 then
    redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now-window)
    redis.call('ZADD',KEYS[2],now,ARGV[9]); failures=redis.call('ZCARD',KEYS[2])
    redis.call('PEXPIRE',KEYS[2],window)
   else failures=failures+1 end
   if state=='half_open' or failures>=threshold then
    state='open'; opened=now; trials=0; successes=0; gen=gen+1
   end
  end
 end
end
redis.call('HSET',KEYS[1],'state',state,'gen',gen,'opened',opened,'failures',failures,
           'trials',trials,'successes',successes)
return {admitted,state,gen,trial,wait,failures,
 redis.call('HGET',KEYS[1],'total_failures') or '0',
 redis.call('HGET',KEYS[1],'total_successes') or '0',
 redis.call('HGET',KEYS[1],'total_rejected') or '0',opened,old}
"""


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
    tag. Backend errors propagate; they are never interpreted as a healthy dependency.
    Half-open reservations expire after recovery_timeout (minimum one second), so a
    crashed probing worker cannot permanently consume the trial slot.
    """

    def __init__(self, client: Any, prefix: str = "python-wrap:breaker:") -> None:
        if not prefix:
            raise ValueError("prefix must not be empty")
        self.client, self.prefix = client, prefix

    def operate(
        self, breaker: Any, operation: str, generation: int = 0, trial: bool = False
    ) -> dict[str, Any]:
        return self._parse(self.client.eval(*self._args(breaker, operation, generation, trial)))

    def _args(self, breaker: Any, operation: str, generation: int, trial: bool) -> tuple[Any, ...]:
        key = self.prefix + "{" + digest(breaker.name) + "}"
        return (
            _BREAKER,
            2,
            key,
            key + ":failures",
            operation,
            breaker.failure_threshold,
            0 if breaker.window is None else max(1, math.ceil(breaker.window * 1000)),
            math.ceil(breaker.recovery_timeout * 1000),
            breaker.half_open_max_calls,
            breaker.success_threshold,
            generation,
            int(trial),
            uuid.uuid4().hex,
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
        )
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

    async def operate(  # type: ignore[override]
        self, breaker: Any, operation: str, generation: int = 0, trial: bool = False
    ) -> dict[str, Any]:
        return self._parse(
            await self.client.eval(*self._args(breaker, operation, generation, trial))
        )


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
