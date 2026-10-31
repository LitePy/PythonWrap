"""Redis coordination backends for circuit breakers and bulkheads."""

from __future__ import annotations

import asyncio
import contextvars
import math
import threading
import uuid
from typing import Any

from python_wrap import _core, _fork
from python_wrap._runtime import digest, seconds
from python_wrap.coordination import AlreadyRunning, Lease
from python_wrap.errors import BulkheadFull

__all__ = ["RedisBreakerState", "SharedBulkhead"]

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
        key = self.prefix + "{" + digest(breaker.name) + "}"
        row = self.client.eval(
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
            lease.__exit__(typ, exc, tb)
        finally:
            with self._lock:
                self._active -= 1

    async def acquire_async(self) -> None:
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
            await lease.__aexit__(typ, exc, tb)
        finally:
            with self._lock:
                self._active -= 1
