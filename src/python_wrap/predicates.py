"""Composable exception predicates without importing HTTP or database drivers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["Predicate", "in_cause_chain", "message_contains", "sqlstate"]


class Predicate:
    """A callable predicate supporting short-circuit |, &, and ~ composition."""

    def __init__(self, test: Callable[[Any], bool]) -> None:
        self.test = test

    def __call__(self, value: Any) -> bool:
        return bool(self.test(value))

    def __or__(self, other: Callable[[Any], bool]) -> Predicate:
        return Predicate(lambda value: self(value) or other(value))

    def __and__(self, other: Callable[[Any], bool]) -> Predicate:
        return Predicate(lambda value: self(value) and other(value))

    def __invert__(self) -> Predicate:
        return Predicate(lambda value: not self(value))


def message_contains(*fragments: str, case_sensitive: bool = False) -> Predicate:
    """Match any fragment in an exception's message (case-insensitive by default)."""
    if not fragments or any(not text for text in fragments):
        raise ValueError("provide nonempty message fragments")
    needles = fragments if case_sensitive else tuple(text.casefold() for text in fragments)
    return Predicate(
        lambda exc: any(
            text in (str(exc) if case_sensitive else str(exc).casefold()) for text in needles
        )
    )


def sqlstate(*prefixes: str) -> Predicate:
    """Match SQLSTATE prefixes on .sqlstate, .pgcode, or DB-API args[0]."""
    if not prefixes or any(not value for value in prefixes):
        raise ValueError("provide SQLSTATE prefixes")

    def test(exc: Any) -> bool:
        args = getattr(exc, "args", ())
        values = (
            getattr(exc, "sqlstate", None),
            getattr(exc, "pgcode", None),
            args[0] if args else None,
        )
        return any(isinstance(value, str) and value.startswith(prefixes) for value in values)

    return Predicate(test)


def in_cause_chain(test: Callable[[Any], bool]) -> Predicate:
    """Walk cause/context and driver .orig links, including cycles, once per exception."""

    def matches(exc: Any) -> bool:
        seen: set[int] = set()
        queue = [exc]
        while queue:
            current = queue.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if test(current):
                return True
            queue.extend(
                getattr(current, field, None) for field in ("__cause__", "__context__", "orig")
            )
        return False

    return Predicate(matches)
