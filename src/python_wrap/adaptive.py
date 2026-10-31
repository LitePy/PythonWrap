"""Adaptive concurrency limits and per-key limiter pools.

* `AdaptiveLimiter` — an AIMD (additive increase, multiplicative decrease) concurrency
  limit that finds a safe level by itself and sheds the excess with `LoadShed`.
* `PerKeyLimiter` — lazily creates one limiter per key (tenant, user, host) with LRU
  eviction of idle keys.
* `per_key` — decorator applying a per-key limiter (any python-wrap limiter or
  bulkhead) to each call.

A fixed `Bulkhead` needs a number picked in advance. An adaptive limit starts at
`initial`, creeps up while calls succeed quickly and backs off when calls time out, drop
connections or get slow, so a struggling dependency sees less load instead of more.
"""

from __future__ import annotations

import functools
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Generic, Literal, NoReturn, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap.errors import BulkheadFull

__all__ = ["AdaptiveLimiter", "AdaptiveStats", "LoadShed", "PerKeyLimiter", "Permit", "per_key"]

F = TypeVar("F", bound=Callable[..., Any])
L = TypeVar("L")

Priority = Literal["low", "normal", "critical"]
_PRIORITIES = ("low", "normal", "critical")
_DEFAULT_ON: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, BulkheadFull)


class LoadShed(BulkheadFull):
    """The adaptive limit is reached, so the call was rejected without being attempted.

    A `BulkheadFull`, so code that already handles bulkhead rejections handles this too.

    Attributes:
        limit: The concurrency limit at that moment.
        in_flight: Calls running at that moment.
        priority: The priority the rejected call asked for.
    """

    def __init__(
        self,
        message: str,
        *,
        name: str | None,
        limit: int,
        in_flight: int,
        priority: str,
    ) -> None:
        super().__init__(message, name=name, max_concurrent=limit)
        self.limit = limit
        self.in_flight = in_flight
        self.priority = priority


@dataclass(frozen=True)
class AdaptiveStats:
    """A snapshot of an `AdaptiveLimiter`.

    Attributes:
        limit: Current concurrency limit.
        in_flight: Calls running now.
        admitted: Calls let in since creation.
        shed: Calls rejected with `LoadShed`.
        successes: Calls that finished well.
        drops: Calls that signalled overload (matching error or too slow).
        increases: How many times the limit went up.
        decreases: How many times the limit went down.
    """

    limit: int
    in_flight: int
    admitted: int
    shed: int
    successes: int
    drops: int
    increases: int
    decreases: int


class Permit:
    """One admitted call. Release it exactly once, or use it as a context manager.

    As a context manager it measures the block: leaving it with an exception that
    matches the limiter's `on` types counts as an overload signal, leaving it cleanly
    counts as a success (unless the block was slower than `target_latency`), and any
    other exception releases the slot without influencing the limit.
    """

    __slots__ = ("_limiter", "_released", "_started", "priority")

    def __init__(self, limiter: AdaptiveLimiter, started: float, priority: str) -> None:
        self._limiter = limiter
        self._started = started
        self._released = False
        self.priority = priority

    def release(self, *, error: BaseException | None = None, ignore: bool = False) -> None:
        """Give the slot back and feed the outcome to the limiter.

        Args:
            error: The exception the call ended with, if any.
            ignore: Release without influencing the limit (e.g. a client error).
        """
        if self._released:
            return
        self._released = True
        self._limiter._finish(self._started, error, ignore)

    def __enter__(self) -> Permit:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release(error=exc)

    async def __aenter__(self) -> Permit:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release(error=exc)


class AdaptiveLimiter:
    """An AIMD concurrency limit that adapts to how the protected resource is doing.

    Every call asks for a slot with `acquire()` (or goes through the decorator). If
    `in_flight` is already at the limit the call is shed at once with `LoadShed`; no
    queueing. Finished calls steer the limit:

    * success (no error, and under `target_latency` when one is set): after `window`
      successful samples (default: the current limit) the limit grows by `increase`;
    * overload signal (an exception matching `on`, or a latency above `target_latency`):
      the limit is multiplied by `backoff_ratio` and the sample count restarts.

    Exceptions that do not match `on` (a validation error, say) release the slot and
    leave the limit alone. A burst of overload signals compounds, so the limit drops
    quickly; the floor is `min_limit`.

    Thread-safe and usable from several event loops (it never blocks). Time comes from
    `python_wrap._core.monotonic`, so `fake_clock` controls latency in tests.

    Args:
        initial: Starting limit.
        min_limit: Lowest the limit may fall to (>= 1).
        max_limit: Highest the limit may rise to.
        target_latency: Seconds above which a finished call counts as overload. `None`
            judges by errors only.
        backoff_ratio: Multiplier applied on overload, in (0, 1).
        increase: Amount added after a full window of successes (> 0).
        window: Successful samples needed per increase. `None` uses the current limit.
        low_priority_share: Fraction of the limit at which `priority="low"` calls are
            shed.
        on: Exception types that signal overload.
        name: Label for events and error messages.

    Raises:
        ValueError: An argument is out of range.

    Example:
        ```python
        from python_wrap.adaptive import AdaptiveLimiter, LoadShed

        limiter = AdaptiveLimiter(initial=2, min_limit=1, max_limit=4, name="search")

        @limiter
        def search(q: str) -> str:
            return q.upper()

        assert search("a") == "A"
        first = limiter.acquire()
        second = limiter.acquire()
        try:
            limiter.acquire()          # two calls are in flight: this one is shed
        except LoadShed as shed:
            assert shed.limit == 2
        first.release(); second.release()
        ```
    """

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def __init__(
        self,
        *,
        initial: int = 10,
        min_limit: int = 1,
        max_limit: int = 200,
        target_latency: float | None = None,
        backoff_ratio: float = 0.9,
        increase: float = 1,
        window: int | None = None,
        low_priority_share: float = 0.8,
        on: tuple[type[BaseException], ...] = _DEFAULT_ON,
        name: str | None = None,
    ) -> None:
        if min_limit < 1:
            raise ValueError(f"min_limit must be >= 1, got {min_limit}")
        if max_limit < min_limit:
            raise ValueError(f"max_limit must be >= min_limit, got {max_limit}")
        if not min_limit <= initial <= max_limit:
            raise ValueError(f"initial must be between {min_limit} and {max_limit}, got {initial}")
        if target_latency is not None and target_latency <= 0:
            raise ValueError(f"target_latency must be > 0 or None, got {target_latency}")
        if not 0 < backoff_ratio < 1:
            raise ValueError(f"backoff_ratio must be in (0, 1), got {backoff_ratio}")
        if increase <= 0:
            raise ValueError(f"increase must be > 0, got {increase}")
        if window is not None and window < 1:
            raise ValueError(f"window must be >= 1 or None, got {window}")
        if not 0 < low_priority_share <= 1:
            raise ValueError(f"low_priority_share must be in (0, 1], got {low_priority_share}")
        self.min_limit = min_limit
        self.max_limit = max_limit
        self.target_latency = target_latency
        self.backoff_ratio = backoff_ratio
        self.increase = increase
        self.window = window
        self.low_priority_share = low_priority_share
        self.on = tuple(on)
        self.name = name
        self._initial = initial
        self._lock = _fork.ForkSafeLock()
        self._reset_state()
        _fork.register(self)

    def _reset_state(self) -> None:
        self._limit = float(self._initial)
        self._in_flight = 0
        self._samples = 0
        self._admitted = 0
        self._shed = 0
        self._successes = 0
        self._drops = 0
        self._increases = 0
        self._decreases = 0

    def _reset_after_fork(self) -> None:
        self._reset_state()

    # -- introspection ------------------------------------------------------------------

    @property
    def limit(self) -> int:
        """The current concurrency limit (whole calls)."""
        with self._lock:
            return self._whole()

    @property
    def in_flight(self) -> int:
        """Calls currently holding a slot."""
        with self._lock:
            return self._in_flight

    @property
    def stats(self) -> AdaptiveStats:
        """A consistent snapshot of the counters."""
        with self._lock:
            return AdaptiveStats(
                limit=self._whole(),
                in_flight=self._in_flight,
                admitted=self._admitted,
                shed=self._shed,
                successes=self._successes,
                drops=self._drops,
                increases=self._increases,
                decreases=self._decreases,
            )

    def __repr__(self) -> str:
        return (
            f"AdaptiveLimiter(name={self.name!r}, limit={self.limit}, in_flight={self.in_flight})"
        )

    def _whole(self) -> int:
        return max(self.min_limit, int(self._limit))

    def _label(self) -> str:
        return self.name or "adaptive"

    # -- admission ----------------------------------------------------------------------

    def acquire(self, *, priority: Priority = "normal") -> Permit:
        """Take a slot now, or raise `LoadShed`. Never blocks.

        `priority="low"` calls are shed once `in_flight >= limit * low_priority_share`;
        `"normal"` and `"critical"` calls only at the full limit. Keep `"critical"` for
        calls you would rather fail than drop (health checks, payments); it is the
        label operators see in `LoadShed.priority` and in events.

        Args:
            priority: `"low"`, `"normal"` or `"critical"`.

        Returns:
            A `Permit`; call `release()` on it or use it in a `with` / `async with`.

        Raises:
            LoadShed: The limit (or the low-priority share of it) is reached.
            ValueError: Unknown priority.
        """
        if priority not in _PRIORITIES:
            raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}")
        with self._lock:
            limit = self._whole()
            threshold = (
                max(1.0, limit * self.low_priority_share) if priority == "low" else float(limit)
            )
            in_flight = self._in_flight
            shed = in_flight >= threshold
            if shed:
                self._shed += 1
            else:
                self._in_flight += 1
                self._admitted += 1
        if shed:
            _core.emit("adaptive.shed", self._label(), limit=limit, in_flight=in_flight)
            label = f"limiter {self.name!r}" if self.name else "limiter"
            raise LoadShed(
                f"{label} shed a {priority} call: {in_flight} in flight, limit {limit}",
                name=self.name,
                limit=limit,
                in_flight=in_flight,
                priority=priority,
            )
        return Permit(self, _core.monotonic(), priority)

    def _finish(self, started: float, error: BaseException | None, ignore: bool) -> None:
        latency = _core.monotonic() - started
        overload = False
        success = False
        if not ignore:
            if error is not None:
                overload = isinstance(error, self.on)
            elif self.target_latency is not None and latency > self.target_latency:
                overload = True
            else:
                success = True
        old = new = 0
        with self._lock:
            old = self._whole()
            self._in_flight = max(0, self._in_flight - 1)
            if overload:
                self._drops += 1
                self._samples = 0
                self._limit = max(float(self.min_limit), self._limit * self.backoff_ratio)
                if self._whole() < old:
                    self._decreases += 1
            elif success:
                self._successes += 1
                self._samples += 1
                needed = self.window if self.window is not None else old
                if self._samples >= needed:
                    self._samples = 0
                    self._limit = min(float(self.max_limit), self._limit + self.increase)
                    if self._whole() > old:
                        self._increases += 1
            new = self._whole()
        if new != old:
            _core.emit("adaptive.limit_changed", self._label(), old=old, new=new)

    # -- decorator ----------------------------------------------------------------------

    def __call__(self, func: F, /) -> F:
        """Guard every call of `func` (sync or `async def`) at normal priority."""
        return self.guard()(func)

    def guard(self, *, priority: Priority = "normal") -> Callable[[F], F]:
        """A decorator guarding calls at a fixed `priority`.

        Example:
            ```python
            from python_wrap.adaptive import AdaptiveLimiter

            limiter = AdaptiveLimiter(initial=5)

            @limiter.guard(priority="low")
            def report() -> int:
                return 1

            assert report() == 1
            ```
        """
        if priority not in _PRIORITIES:
            raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}")

        def decorate(func: F) -> F:
            if _core.is_async(func):

                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    async with self.acquire(priority=priority):
                        return await func(*args, **kwargs)

                async_wrapper.__python_wrap_layer__ = ("adaptive", self)  # type: ignore[attr-defined]
                return cast(F, async_wrapper)

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.acquire(priority=priority):
                    return func(*args, **kwargs)

            wrapper.__python_wrap_layer__ = ("adaptive", self)  # type: ignore[attr-defined]
            return cast(F, wrapper)

        return decorate


# ------------------------------------------------------------------------- per key


def _busy(obj: Any) -> bool:
    """True if a pooled limiter has calls running (so it must not be evicted)."""
    for attr in ("in_flight", "active"):
        value = getattr(obj, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return False


class PerKeyLimiter(Generic[L]):
    """A pool of limiters, one per key, created on first use.

    Use it to give every tenant, user or upstream host its own limit so one noisy key
    cannot starve the rest. Keys are kept in least-recently-used order; when more than
    `max_keys` exist, the oldest *idle* ones (nothing in flight) are dropped, so memory
    stays bounded. A dropped key simply starts fresh the next time it is seen.

    Args:
        factory: Called with the key to build its limiter, e.g.
            `lambda key: Bulkhead(5, max_waiting=0)`.
        max_keys: Most keys kept before idle ones are evicted (>= 1).

    Raises:
        ValueError: `max_keys` is below 1.

    Example:
        ```python
        from python_wrap.adaptive import AdaptiveLimiter, PerKeyLimiter

        pool = PerKeyLimiter(lambda tenant: AdaptiveLimiter(initial=3), max_keys=100)
        assert pool.get("acme") is pool.get("acme")
        assert pool.get("acme") is not pool.get("globex")
        assert len(pool) == 2
        ```
    """

    def __init__(self, factory: Callable[[Hashable], L], *, max_keys: int = 10_000) -> None:
        if max_keys < 1:
            raise ValueError(f"max_keys must be >= 1, got {max_keys}")
        self._factory = factory
        self.max_keys = max_keys
        self._items: OrderedDict[Hashable, L] = OrderedDict()
        self._lock = _fork.ForkSafeLock()
        _fork.register(self)

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def _reset_after_fork(self) -> None:
        self._items.clear()

    def get(self, key: Hashable) -> L:
        """The limiter for `key`, created if needed and marked most recently used."""
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
                return item
            item = self._factory(key)
            self._items[key] = item
            self._evict()
            return item

    def _evict(self) -> None:
        if len(self._items) <= self.max_keys:
            return
        for key in list(self._items):
            if len(self._items) <= self.max_keys:
                break
            if not _busy(self._items[key]):
                del self._items[key]

    def discard(self, key: Hashable) -> bool:
        """Forget `key`. Returns True if it existed."""
        with self._lock:
            return self._items.pop(key, None) is not None

    def clear(self) -> None:
        """Forget every key."""
        with self._lock:
            self._items.clear()

    def keys(self) -> list[Hashable]:
        """Current keys, least recently used first."""
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items

    def __getitem__(self, key: Hashable) -> L:
        return self.get(key)


class _Slot:
    """A key's limiter plus the function wrapped by it."""

    __slots__ = ("limiter", "wrapped")

    def __init__(self, limiter: Any, wrapped: Callable[..., Any]) -> None:
        self.limiter = limiter
        self.wrapped = wrapped

    @property
    def in_flight(self) -> int:
        return 1 if _busy(self.limiter) else 0


def per_key(
    limiter_factory: Callable[[Hashable], Any],
    *,
    key: Callable[..., Hashable],
    max_keys: int = 10_000,
) -> Callable[[F], F]:
    """Apply a separate limiter to each distinct key of the call.

    `limiter_factory(key)` must return something that works as a decorator, which is
    true of `Bulkhead`, `AdaptiveLimiter` and `RateLimiter`. The decorated function
    keeps its sync or async nature. The pool is bounded and LRU-evicted like
    `PerKeyLimiter`; the pool is reachable as `wrapped.limiters`.

    (`rate_limit(..., key=...)` and `RateLimiter` already split their window per key
    themselves; use `per_key` for bulkheads and adaptive limiters, or when each key
    needs a different configuration.)

    Args:
        limiter_factory: Builds the limiter for a key.
        key: Called with the call's arguments; returns the hashable key.
        max_keys: Most keys kept before idle ones are evicted.

    Returns:
        A decorator.

    Example:
        ```python
        from python_wrap.adaptive import per_key
        from python_wrap.bulkhead import Bulkhead

        @per_key(lambda tenant: Bulkhead(2, max_waiting=0), key=lambda tenant, **_: tenant)
        def handle(tenant: str) -> str:
            return tenant

        assert handle("acme") == "acme"
        assert "acme" in handle.limiters  # type: ignore[attr-defined]
        ```
    """

    def decorate(func: F) -> F:
        def make(k: Hashable) -> _Slot:
            limiter = limiter_factory(k)
            return _Slot(limiter, limiter(func))

        pool: PerKeyLimiter[_Slot] = PerKeyLimiter(make, max_keys=max_keys)

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                slot = pool.get(key(*args, **kwargs))
                return await slot.wrapped(*args, **kwargs)

            async_wrapper.limiters = pool  # type: ignore[attr-defined]
            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            slot = pool.get(key(*args, **kwargs))
            return slot.wrapped(*args, **kwargs)

        wrapper.limiters = pool  # type: ignore[attr-defined]
        return cast(F, wrapper)

    return decorate
