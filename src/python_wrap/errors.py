"""Exceptions raised by python-wrap.

Each one also subclasses the closest built-in, so ``except TimeoutError`` or
``except ValueError`` keeps working for code written against v0.1.
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


__all__ = [
    "ArgumentTypeError",
    "CircuitOpenError",
    "PythonWrapError",
    "RateLimitExceeded",
    "TimeoutExpired",
    "ValidationError",
]
