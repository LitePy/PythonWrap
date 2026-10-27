"""Deadlines: one time budget shared by every call inside a block.

`deadline(seconds)` records an absolute point in time in a `contextvars.ContextVar`,
so it follows the code it wraps across function calls, `await`s and tasks created
inside the block (each task copies the context it was created in). Inner code asks
`remaining()` how much time is left, or calls `check()` to fail fast once it is gone.

A deadline is cooperative: it never interrupts anything by itself. The toolkit's
`timeout` (decorator or context manager) clamps its own limit to the remaining deadline,
so a `timeout(30)` inside `deadline(5)` gives up after at most 5 seconds. Your own code
can do the same, e.g. pass `remaining()` as an HTTP client timeout.

Nested deadlines never extend an outer one: the earliest wins.
"""

from __future__ import annotations

import contextvars
from types import TracebackType

from python_wrap import _core
from python_wrap.errors import TimeoutExpired

__all__ = ["Deadline", "check", "deadline", "remaining"]

# (absolute expiry on the ``_core.monotonic()`` clock, seconds the setting block asked for)
_current: contextvars.ContextVar[tuple[float, float] | None] = contextvars.ContextVar(
    "python_wrap_deadline", default=None
)


class Deadline:
    """A time budget for a block of code; create it with `deadline`.

    Use as `with deadline(5):` or `async with deadline(5):`. Entering computes the
    absolute expiry; if an enclosing deadline expires sooner, that one stays in force.
    One instance may be entered several times, but each `with` must exit in the reverse
    order of entering (as `with` blocks naturally do in one thread or task).

    Attributes:
        seconds: The budget this deadline was created with.
    """

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"deadline seconds must be >= 0, got {seconds!r}")
        self.seconds = seconds
        self._tokens: list[contextvars.Token[tuple[float, float] | None]] = []

    @property
    def expires_at(self) -> float | None:
        """The effective expiry (on the `_core.monotonic()` clock) in the current context.

        `None` when no deadline is active here.
        """
        current = _current.get()
        return None if current is None else current[0]

    def __enter__(self) -> Deadline:
        expiry = _core.monotonic() + self.seconds
        outer = _current.get()
        # An enclosing deadline that comes first stays in force.
        keep_outer = outer is not None and outer[0] <= expiry
        value = outer if keep_outer and outer is not None else (expiry, self.seconds)
        self._tokens.append(_current.set(value))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _current.reset(self._tokens.pop())

    async def __aenter__(self) -> Deadline:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __repr__(self) -> str:
        return f"Deadline(seconds={self.seconds!r}, remaining={remaining()!r})"


def deadline(seconds: float) -> Deadline:
    """Give the enclosed block (and everything it calls) at most ``seconds`` seconds.

    The deadline is stored in a context variable, so it is visible to every function
    called inside the block, sync or async, and to tasks started from it. Nesting never
    extends time: an inner `deadline(10)` inside `deadline(2)` still ends after 2 seconds.

    Nothing is interrupted automatically. `timeout` clamps to the remaining time, and
    your code can call `remaining()` or `check()`.

    Args:
        seconds: The budget in seconds (>= 0).

    Returns:
        A `Deadline` to use with `with` or `async with`.

    Raises:
        ValueError: If ``seconds`` is negative.

    Example:
        ```python
        from python_wrap.deadline import deadline, remaining

        assert remaining() is None
        with deadline(5):
            with deadline(60):  # cannot extend the outer budget
                left = remaining()
                assert left is not None and left <= 5
        assert remaining() is None
        ```
    """
    return Deadline(seconds)


def remaining() -> float | None:
    """Seconds left before the current deadline, or `None` if there is none.

    Never negative: an expired deadline reports `0.0`.

    Returns:
        The remaining time in seconds, or `None` outside any `deadline` block.

    Example:
        ```python
        from python_wrap.deadline import deadline, remaining

        with deadline(2):
            budget = remaining()  # e.g. pass as an HTTP client timeout
            assert budget is not None and 0 < budget <= 2
        ```
    """
    current = _current.get()
    if current is None:
        return None
    return max(current[0] - _core.monotonic(), 0.0)


def check() -> None:
    """Raise `TimeoutExpired` if the current deadline has passed; otherwise do nothing.

    Call it between steps of a long job to stop as soon as the budget is gone.

    Raises:
        TimeoutExpired: The deadline has passed. Its ``seconds`` is the budget of the
            `deadline` block that set it.

    Example:
        ```python
        from python_wrap.deadline import check, deadline

        with deadline(5):
            for step in range(3):
                check()  # raises TimeoutExpired once the 5 seconds are used up
        ```
    """
    current = _current.get()
    if current is not None and _core.monotonic() >= current[0]:
        raise TimeoutExpired(f"deadline of {current[1]:g}s has passed", seconds=current[1])
