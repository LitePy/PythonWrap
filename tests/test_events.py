from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from python_wrap import _core
from python_wrap.events import EVENT_NAMES, Event, filter_events, listening, subscribe


@pytest.fixture(autouse=True)
def _clean_listeners() -> Iterator[None]:
    assert _core.listeners == []
    yield
    _core.listeners.clear()


def test_subscribe_receives_and_unsubscribe_stops() -> None:
    seen: list[Event] = []
    unsubscribe = subscribe(seen.append)
    _core.emit("cache.hit", "f")
    unsubscribe()
    unsubscribe()  # idempotent
    _core.emit("cache.hit", "f")
    assert seen == [Event("cache.hit", "f", {})]
    assert _core.listeners == []


def test_unsubscribe_twice_does_not_remove_a_second_registration() -> None:
    seen: list[Event] = []
    first = subscribe(seen.append)
    subscribe(seen.append)
    first()
    first()
    _core.emit("cache.miss", "f")
    assert len(seen) == 1


def test_listening_context_manager() -> None:
    seen: list[Event] = []
    with listening(seen.append):
        _core.emit("retry.succeeded", "f", attempts=2)
    _core.emit("retry.succeeded", "f", attempts=3)
    assert [e.fields for e in seen] == [{"attempts": 2}]


def test_listening_unsubscribes_on_error() -> None:
    with pytest.raises(RuntimeError), listening(lambda e: None):
        raise RuntimeError
    assert _core.listeners == []


def test_filter_events_by_prefix() -> None:
    seen: list[str] = []
    with listening(filter_events("retry.", lambda e: seen.append(e.name))):
        _core.emit("retry.gave_up", "f")
        _core.emit("cache.hit", "f")
    with listening(filter_events(("cache.", "bulkhead."), lambda e: seen.append(e.name))):
        _core.emit("cache.miss", "f")
        _core.emit("bulkhead.rejected", "f")
        _core.emit("throttle.skipped", "f")
    assert seen == ["retry.gave_up", "cache.miss", "bulkhead.rejected"]


def test_failing_listener_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Event] = []

    def bad(event: Event) -> None:
        raise ValueError("boom")

    with listening(bad), listening(seen.append):
        _core.emit("cache.hit", "f")
    assert len(seen) == 1
    assert caplog.records[0].levelno == logging.ERROR


def test_unsubscribe_during_emit_is_safe() -> None:
    seen: list[str] = []
    unsubscribe_once = subscribe(lambda e: (seen.append("once"), unsubscribe_once()))
    subscribe(lambda e: seen.append("other"))
    _core.emit("cache.hit", "f")
    assert seen == ["once", "other"]


def test_event_names_catalogue() -> None:
    assert "retry.attempt_failed" in EVENT_NAMES
    assert all(isinstance(v, str) and v for v in EVENT_NAMES.values())
    assert len(EVENT_NAMES) == 14
