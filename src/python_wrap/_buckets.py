"""Per-key state buckets shared by the rate limiter, throttle and debounce."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Hashable
from typing import (
    Any,
    Generic,
    ParamSpec,
    TypeVar,
)

from python_wrap import _fork

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")

_SWEEP_BUCKETS_EVERY = 1024


class _Buckets(Generic[S]):
    """Decorator state split by ``key(*args, **kwargs)`` and/or by the instance (``args[0]``).

    With neither ``key`` nor ``per_instance`` there is a single state, so the decorator
    behaves as it always did. With ``per_instance`` each instance gets its own states,
    found by identity through a weak reference: instances need not be hashable, are not
    kept alive, and their state goes with them. Keyed states for which ``idle(state)`` is
    true are dropped every ``_SWEEP_BUCKETS_EVERY`` lookups, so the keys cannot pile up.
    """

    def __init__(
        self,
        factory: Callable[[], S],
        *,
        key: Callable[..., Hashable] | None = None,
        per_instance: bool = False,
        idle: Callable[[S], bool] | None = None,
        what: str = "decorator",
    ) -> None:
        self._factory = factory
        self._key = key
        self._per_instance = per_instance
        self._idle = idle
        self._what = what
        self._lock = _fork.ForkSafeLock()
        self._plain: dict[Hashable, S] = {}
        # per_instance: id(instance) -> (weak reference, that instance's keyed states)
        self._instances: dict[int, tuple[weakref.ref[Any], dict[Hashable, S]]] = {}
        self._lookups = 0
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._plain.clear()
        self._instances.clear()
        self._lookups = 0

    def _scope(self, args: tuple[Any, ...]) -> dict[Hashable, S]:
        if not self._per_instance:
            return self._plain
        if not args:
            raise TypeError(
                f"{self._what}(per_instance=True): call the function with the instance "
                "as its first positional argument"
            )
        obj = args[0]
        i = id(obj)
        slot = self._instances.get(i)
        if slot is not None and slot[0]() is obj:
            return slot[1]

        def forget(ref: weakref.ref[Any], i: int = i) -> None:
            # Runs while the instance is destroyed, maybe in a thread holding the lock:
            # no locking here (dict operations are atomic). The id cannot be reused
            # before this callback has run.
            current = self._instances.get(i)
            if current is not None and current[0] is ref:
                self._instances.pop(i, None)

        try:
            ref = weakref.ref(obj, forget)
        except TypeError:
            raise TypeError(
                f"{self._what}(per_instance=True) needs weak-referenceable instances, but "
                f"{type(obj).__name__!r} is not (a __slots__ class must also list "
                "'__weakref__')"
            ) from None
        with self._lock:
            slot = self._instances.get(i)
            if slot is not None and slot[0]() is obj:
                return slot[1]
            states: dict[Hashable, S] = {}
            self._instances[i] = (ref, states)
        return states

    def get(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> S:
        """The state for this call, created on first use."""
        k = None if self._key is None else self._key(*args, **kwargs)
        scope = self._scope(args)
        with self._lock:
            state = scope.get(k)
            if state is None:
                state = scope[k] = self._factory()
            if self._key is not None and self._idle is not None:
                self._lookups += 1
                if self._lookups >= _SWEEP_BUCKETS_EVERY:
                    self._sweep()
            return state

    def _sweep(self) -> None:
        # Caller holds the lock.
        self._lookups = 0
        idle = self._idle
        assert idle is not None
        scopes = (
            [self._plain] if not self._per_instance else [s for _, s in self._instances.values()]
        )
        for scope in scopes:
            for k in [k for k, state in scope.items() if idle(state)]:
                del scope[k]

    def states(self) -> list[S]:
        """Every live state (a snapshot)."""
        with self._lock:
            scopes = (
                [self._plain]
                if not self._per_instance
                else [s for ref, s in self._instances.values() if ref() is not None]
            )
            return [state for scope in scopes for state in scope.values()]

    def find(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> S | None:
        """The existing state for these call arguments, or ``None`` (never creates one)."""
        k = None if self._key is None else self._key(*args, **kwargs)
        if self._per_instance:
            if not args:
                return None
            slot = self._instances.get(id(args[0]))
            if slot is None or slot[0]() is not args[0]:
                return None
            scope = slot[1]
        else:
            scope = self._plain
        with self._lock:
            return scope.get(k)

    @property
    def selective(self) -> bool:
        """True when ``cancel``/``flush`` need the call's arguments to pick a state."""
        return self._key is not None or self._per_instance

    def clear(self) -> None:
        with self._lock:
            self._plain.clear()
            self._instances.clear()


# ---------------------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------------------
