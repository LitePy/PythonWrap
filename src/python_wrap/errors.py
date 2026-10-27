"""Exceptions raised by python-wrap.

Every one subclasses `PythonWrapError`. Those with a natural built-in counterpart also
subclass it (`TimeoutExpired` is a `TimeoutError`, `ValidationError` a `ValueError`,
`ArgumentTypeError` a `TypeError`), so ``except TimeoutError`` or ``except ValueError``
keeps working for code written against v0.1.
"""

from __future__ import annotations


class PythonWrapError(Exception):
    """Base class for every exception this package raises."""


class TimeoutExpired(PythonWrapError, TimeoutError):
    """The wrapped call did not finish within its time limit."""

    def __init__(self, message: str, *, seconds: float) -> None:
        super().__init__(message)
        self.seconds = seconds


class RateLimitExceeded(PythonWrapError):
    """The call was rejected because its rate limit is used up."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CircuitOpenError(PythonWrapError):
    """The circuit breaker is open, so the call was not attempted."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BulkheadFull(PythonWrapError):
    """A bulkhead is at capacity: its wait queue is full, or waiting for a slot timed out."""

    def __init__(self, message: str, *, name: str | None, max_concurrent: int) -> None:
        super().__init__(message)
        self.name = name
        self.max_concurrent = max_concurrent


class ValidationError(PythonWrapError, ValueError):
    """An argument failed a validator."""

    def __init__(self, message: str, *, parameter: str, value: object) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.value = value


class ArgumentTypeError(PythonWrapError, TypeError):
    """An argument does not match its expected type."""

    def __init__(self, message: str, *, parameter: str, expected: object, value: object) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.expected = expected
        self.value = value


class BatchError(PythonWrapError):
    """A batch function broke its output contract for this caller's item.

    Raised by ``batch_calls`` when the batch function returns the wrong number of
    results, a mapping without this item's key, or something that is neither a sequence
    nor a mapping.
    """


__all__ = [
    "ArgumentTypeError",
    "BatchError",
    "BulkheadFull",
    "CircuitOpenError",
    "PythonWrapError",
    "RateLimitExceeded",
    "TimeoutExpired",
    "ValidationError",
]
