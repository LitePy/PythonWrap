"""Reusable, named bundles of decorators that always stack in the right order."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])

__all__ = ["ROLES", "Policy", "policy"]

_Decorator = Callable[[Any], Any]

#: Role names, outermost first. The canonical order, with the reason for each position:
#:
#: - ``observe``: timing, logging, audit see the outcome the caller experiences.
#: - ``validate``: reject bad input before anything is retried, cached or counted.
#: - ``fallback``: catches whatever the layers below could not fix.
#: - ``cache``: a hit skips everything below; failures are never cached.
#: - ``limiter``: one rate-limit token per logical call, not per attempt.
#: - ``bulkhead``: caps concurrent logical calls into the dependency.
#: - ``breaker``: counts calls whose retries were exhausted.
#: - ``retry``: repeats everything below it.
#: - ``timeout``: a deadline per attempt.
#: - ``guard``: per-attempt state protection (revert_on_failure, transactional).
ROLES: tuple[str, ...] = (
    "observe",
    "validate",
    "fallback",
    "cache",
    "limiter",
    "bulkhead",
    "breaker",
    "retry",
    "timeout",
    "guard",
)


def _as_tuple(role: str, value: Any) -> tuple[_Decorator, ...]:
    if value is None:
        return ()
    items = tuple(value) if isinstance(value, (list, tuple)) else (value,)
    for item in items:
        if not callable(item):
            raise TypeError(f"policy {role}= must be a decorator or a list of them; got {item!r}")
    return items


def _apply_one(decorator: _Decorator, target: Any) -> Any:
    """Apply a decorator, looking through staticmethod/classmethod descriptors."""
    if isinstance(target, (staticmethod, classmethod)):
        return type(target)(decorator(target.__func__))
    return decorator(target)


class Policy:
    """An immutable, named bundle of decorators applied in a fixed, correct order.

    Create one with `policy`. A `Policy` is itself a decorator, so it works on
    plain functions, ``async def`` functions and methods, and keeps the
    decorated function's type. Roles run outermost first in the order of `ROLES`,
    wherever you pass them; `outer` decorators wrap all roles and `inner`
    decorators sit inside all roles, each group in the order given.

    Example:
        ```python
        resilient = policy(retry=retry(max_attempts=3), timeout=timeout(2), name="io")

        @resilient
        def fetch() -> str: ...
        ```
    """

    __slots__ = ("_inner", "_name", "_outer", "_roles")

    _name: str | None
    _outer: tuple[_Decorator, ...]
    _inner: tuple[_Decorator, ...]
    _roles: dict[str, tuple[_Decorator, ...]]

    def __init__(
        self,
        *,
        name: str | None,
        outer: tuple[_Decorator, ...],
        inner: tuple[_Decorator, ...],
        roles: Mapping[str, tuple[_Decorator, ...]],
    ) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_outer", outer)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_roles", dict(roles))

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("Policy is immutable; use .with_(...) to derive a variant")

    def __delattr__(self, key: str) -> None:
        raise AttributeError("Policy is immutable; use .with_(...) to derive a variant")

    @property
    def name(self) -> str | None:
        """The policy name, if any."""
        return self._name

    @property
    def order(self) -> tuple[str, ...]:
        """The final layer names, outermost first (``outer``, roles, ``inner``)."""
        names: list[str] = ["outer"] * len(self._outer)
        for role in ROLES:
            names += [role] * len(self._roles.get(role, ()))
        names += ["inner"] * len(self._inner)
        return tuple(names)

    def _layers(self) -> list[_Decorator]:
        layers: list[_Decorator] = list(self._outer)
        for role in ROLES:
            layers.extend(self._roles.get(role, ()))
        layers.extend(self._inner)
        return layers

    def apply(self, func: F) -> F:
        """Decorate `func` with every layer in the policy's order.

        Args:
            func: A function, ``async def`` function, or static/class method.

        Returns:
            The decorated function (same type as `func`).
        """
        result: Any = func
        for decorator in reversed(self._layers()):
            result = _apply_one(decorator, result)
        return cast(F, result)

    def __call__(self, func: F) -> F:
        return self.apply(func)

    def with_(self, **changes: Any) -> Policy:
        """Return a new policy with some roles replaced.

        Args:
            **changes: Any role name, `outer`, `inner` or `name`. A value of
                ``None`` removes that role; otherwise it replaces it.

        Returns:
            A new `Policy`; this one is unchanged.

        Raises:
            TypeError: For an unknown keyword or a non-callable decorator.
        """
        known = {*ROLES, "outer", "inner", "name"}
        unknown = set(changes) - known
        if unknown:
            raise TypeError(f"unknown policy option(s): {', '.join(sorted(unknown))}")
        roles = dict(self._roles)
        for role in ROLES:
            if role in changes:
                roles[role] = _as_tuple(role, changes[role])
        return Policy(
            name=changes.get("name", self._name),
            outer=_as_tuple("outer", changes["outer"]) if "outer" in changes else self._outer,
            inner=_as_tuple("inner", changes["inner"]) if "inner" in changes else self._inner,
            roles=roles,
        )

    def __repr__(self) -> str:
        order = " > ".join(self.order) or "(empty)"
        label = f"{self._name!r}, " if self._name else ""
        return f"Policy({label}{order})"


def policy(
    *,
    name: str | None = None,
    observe: _Decorator | Sequence[_Decorator] | None = None,
    validate: _Decorator | Sequence[_Decorator] | None = None,
    fallback: _Decorator | Sequence[_Decorator] | None = None,
    cache: _Decorator | Sequence[_Decorator] | None = None,
    limiter: _Decorator | Sequence[_Decorator] | None = None,
    bulkhead: _Decorator | Sequence[_Decorator] | None = None,
    breaker: _Decorator | Sequence[_Decorator] | None = None,
    retry: _Decorator | Sequence[_Decorator] | None = None,
    timeout: _Decorator | Sequence[_Decorator] | None = None,
    guard: _Decorator | Sequence[_Decorator] | None = None,
    outer: _Decorator | Sequence[_Decorator] | None = None,
    inner: _Decorator | Sequence[_Decorator] | None = None,
) -> Policy:
    """Build a reusable `Policy` from decorators placed in named roles.

    Each keyword is a role, and the role (not the keyword order) decides where the
    decorator sits. The canonical order, outermost first, is `ROLES`: ``observe``
    (timing, logging), ``validate``, ``fallback``, ``cache``, ``limiter``
    (rate limits), ``bulkhead``, ``breaker`` (a `CircuitBreaker` or
    `circuit_breaker(...)`), ``retry``, ``timeout``, ``guard``
    (revert_on_failure and similar). The reasons are in the stacking guide.

    A role takes one decorator or a list of them (outermost first). Use `outer`
    and `inner` for any decorator that has no role: `outer` ones wrap every role,
    `inner` ones sit just above the function.

    Args:
        name: Label shown in ``repr``.
        observe: Observers such as `timing` or `audit`.
        validate: Input checks such as `validate_args`.
        fallback: `fallback` or `suppress`.
        cache: `memoize`, `once`, `singleflight`.
        limiter: `rate_limit`, `throttle`.
        bulkhead: `bulkhead` or `concurrency_limit`.
        breaker: A `CircuitBreaker` or `circuit_breaker(...)`.
        retry: `retry(...)`.
        timeout: `timeout(...)`.
        guard: Per-attempt state protection.
        outer: Unclassified decorators placed outside every role.
        inner: Unclassified decorators placed inside every role.

    Returns:
        An immutable `Policy`, usable as a decorator.

    Raises:
        TypeError: If an argument is not callable (or a list of callables).

    Example:
        ```python
        from python_wrap import CircuitBreaker, retry, timeout, policy

        resilient = policy(
            timeout=timeout(2),
            retry=retry(max_attempts=3, on=ConnectionError),
            breaker=CircuitBreaker(failure_threshold=5, on=ConnectionError),
            name="payments",
        )

        @resilient
        def charge(amount: int) -> int:
            return amount
        ```
    """
    given = {
        "observe": observe,
        "validate": validate,
        "fallback": fallback,
        "cache": cache,
        "limiter": limiter,
        "bulkhead": bulkhead,
        "breaker": breaker,
        "retry": retry,
        "timeout": timeout,
        "guard": guard,
    }
    return Policy(
        name=name,
        outer=_as_tuple("outer", outer),
        inner=_as_tuple("inner", inner),
        roles={role: _as_tuple(role, value) for role, value in given.items()},
    )
