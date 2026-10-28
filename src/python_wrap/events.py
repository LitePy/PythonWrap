"""Observe what python-wrap decorators do: retries, cache hits, circuit changes, ...

Decorators publish `Event` objects to a process-wide list of listeners. With no
listener registered, publishing costs a single list check. Use `subscribe` (or the
`listening` context manager) to feed Prometheus, OpenTelemetry, statsd or your logs.
`EVENT_NAMES` is the catalogue of every event and its fields.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from python_wrap import _core
from python_wrap._core import Event

__all__ = ["EVENT_NAMES", "Event", "Listener", "filter_events", "listening", "subscribe"]

Listener = Callable[[Event], None]

#: Event name -> description of its ``fields`` (``Event.function`` is the decorated
#: function's qualified name unless noted). The docs page renders this mapping.
EVENT_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "retry.attempt_failed": (
            "An attempt failed and another will follow. Fields: attempt (int, 1-based), "
            "exception (BaseException), next_delay (float seconds before the next attempt)."
        ),
        "retry.succeeded": "The call finally succeeded. Fields: attempts (int, total made).",
        "retry.gave_up": (
            "Attempts were exhausted. Fields: attempts (int), exception (BaseException)."
        ),
        "timeout.expired": "A call exceeded its time limit. Fields: seconds (float, the limit).",
        "circuit.state_changed": (
            "A circuit breaker moved between states. function is the breaker name. "
            "Fields: old (str), new (str): 'closed', 'open' or 'half_open'."
        ),
        "circuit.rejected": (
            "A call was refused because the circuit is open. Fields: retry_after (float seconds)."
        ),
        "rate_limit.rejected": (
            "A call was refused by a rate limiter. Fields: retry_after (float seconds)."
        ),
        "rate_limit.waited": (
            "A call was delayed by a rate limiter. Fields: seconds (float, time waited)."
        ),
        "throttle.skipped": "A call was skipped by throttle. No fields.",
        "cache.hit": "A cached value was returned. No fields.",
        "cache.miss": "No cached value; the function ran. No fields.",
        "cache.evicted": "An entry left the cache. Fields: reason (str): 'lru' or 'ttl'.",
        "cache.stale": (
            "A stale value was served (stale-while-revalidate) and a background refresh "
            "started. No fields."
        ),
        "cache.refresh_failed": (
            "A background refresh failed; the stale value is kept. "
            "Fields: exception (BaseException)."
        ),
        "bulkhead.rejected": (
            "A call was refused because the bulkhead is full. Fields: active (int), waiting (int)."
        ),
    }
)


def subscribe(listener: Listener) -> Callable[[], None]:
    """Register ``listener`` to receive every `Event`.

    Listeners run synchronously in the thread that triggered the event, so keep them
    fast. An exception raised by a listener is logged and swallowed; it never reaches the
    decorated function. Subscribing the same callable twice delivers each event twice.

    Args:
        listener: Callable taking one `Event`.

    Returns:
        A function that removes this registration. Calling it again does nothing.

    Example:
        >>> seen = []
        >>> unsubscribe = subscribe(seen.append)
        >>> unsubscribe()
    """
    with _core.listeners_lock:
        _core.listeners.append(listener)
    removed = False

    def unsubscribe() -> None:
        nonlocal removed
        with _core.listeners_lock:
            if removed:
                return
            removed = True
            # ``emit`` iterates over a tuple snapshot, so deleting in place is safe.
            for i, item in enumerate(_core.listeners):
                if item is listener:
                    del _core.listeners[i]
                    break

    return unsubscribe


@contextmanager
def listening(listener: Listener) -> Iterator[Listener]:
    """Subscribe ``listener`` for the duration of a ``with`` block.

    Args:
        listener: Callable taking one `Event`.

    Yields:
        The listener, so ``with listening(events.append) as handler`` works.

    Example:
        >>> events = []
        >>> with listening(events.append):
        ...     pass
    """
    unsubscribe = subscribe(listener)
    try:
        yield listener
    finally:
        unsubscribe()


def filter_events(prefix: str | tuple[str, ...], listener: Listener) -> Listener:
    """Wrap ``listener`` so it only receives events whose name starts with ``prefix``.

    Args:
        prefix: A name prefix such as ``"retry."``, or a tuple of prefixes.
        listener: The listener to call for matching events.

    Returns:
        A new listener; pass it to `subscribe`.

    Example:
        >>> retries = []
        >>> unsubscribe = subscribe(filter_events("retry.", retries.append))
        >>> unsubscribe()
    """

    def filtered(event: Event) -> None:
        if event.name.startswith(prefix):
            listener(event)

    return filtered
