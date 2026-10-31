"""Retry timelines on re-raised exceptions, Sentry breadcrumbs and span annotations."""

from __future__ import annotations

import asyncio

import pytest

from python_wrap import (
    AttemptRecord,
    CircuitBreaker,
    attempt_history,
    retry,
)
from python_wrap.observability import annotate_span, sentry_breadcrumbs


def test_gave_up_exception_carries_one_record_per_attempt(fake_clock):
    @retry(max_attempts=3, delay=1, backoff=2, on=ConnectionError)
    def fetch() -> None:
        fake_clock.advance(0.5)
        raise ConnectionError("secret-host refused")

    with pytest.raises(ConnectionError) as info:
        fetch()
    history = attempt_history(info.value)
    assert [r.number for r in history] == [1, 2, 3]
    assert [r.next_delay for r in history] == [1, 2, None]
    assert all(r.exception == "builtins.ConnectionError" for r in history)
    assert all(r.duration == pytest.approx(0.5) for r in history)
    assert "secret-host" not in repr(history)  # messages are never kept


def test_non_retried_exception_records_single_attempt():
    @retry(max_attempts=3, delay=0, on=ConnectionError, retry_if=lambda e: False)
    def fetch() -> None:
        raise ConnectionError

    with pytest.raises(ConnectionError) as info:
        fetch()
    assert [r.number for r in attempt_history(info.value)] == [1]


def test_success_and_unrelated_exceptions_have_no_history():
    assert attempt_history(ValueError()) == ()

    @retry(max_attempts=3, delay=0, on=ConnectionError)
    def bug() -> None:
        raise KeyError("not retried")

    with pytest.raises(KeyError) as info:
        bug()
    assert attempt_history(info.value) == ()


def test_async_history():
    calls = []

    @retry(max_attempts=2, delay=0, on=TimeoutError)
    async def fetch() -> None:
        calls.append(1)
        raise TimeoutError

    with pytest.raises(TimeoutError) as info:
        asyncio.run(fetch())
    assert len(attempt_history(info.value)) == 2 == len(calls)


def test_exception_without_dict_is_reraised_unchanged():
    class Slotted(Exception):
        __slots__ = ()

    @retry(max_attempts=2, delay=0, on=Slotted)
    def fetch() -> None:
        raise Slotted

    with pytest.raises(Slotted):
        fetch()


class FakeSentry:
    def __init__(self) -> None:
        self.crumbs: list[dict[str, object]] = []

    def add_breadcrumb(self, **crumb: object) -> None:
        self.crumbs.append(crumb)


def test_sentry_breadcrumbs_record_safe_event_data():
    sentry = FakeSentry()
    handle = sentry_breadcrumbs(sentry=sentry)
    assert handle is not None
    try:

        @retry(max_attempts=2, delay=0, on=ConnectionError)
        def fetch() -> None:
            raise ConnectionError("password=hunter2")

        with pytest.raises(ConnectionError):
            fetch()
        breaker = CircuitBreaker(failure_threshold=1, name="vendor")
        with pytest.raises(ValueError):
            breaker(lambda: (_ for _ in ()).throw(ValueError()))()
    finally:
        handle.close()
    names = [crumb["message"].split()[0] for crumb in sentry.crumbs]  # type: ignore[union-attr]
    assert names[:2] == ["retry.attempt_failed", "retry.gave_up"]
    assert "circuit.state_changed" in names
    gave_up = sentry.crumbs[1]
    assert gave_up["level"] == "error" and gave_up["category"] == "python_wrap"
    assert gave_up["data"]["exception"] == "ConnectionError"  # type: ignore[index]
    assert "hunter2" not in repr(sentry.crumbs)
    changed = next(c for c in sentry.crumbs if "circuit.state_changed" in str(c["message"]))
    assert changed["data"]["new"] == "open"  # type: ignore[index]
    count = len(sentry.crumbs)
    with pytest.raises(ConnectionError):
        fetch()
    assert len(sentry.crumbs) == count  # closed


def test_sentry_breadcrumbs_without_sentry_installed_is_a_noop(monkeypatch):
    import importlib

    def missing(name: str, package: str | None = None) -> object:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    assert sentry_breadcrumbs() is None


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, dict[str, object]]] = []

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, object]) -> None:
        self.events.append((name, attributes))


def test_annotate_span_with_timeline():
    @retry(max_attempts=2, delay=0, on=OSError)
    def fetch() -> None:
        raise OSError

    with pytest.raises(OSError) as info:
        fetch()
    span = FakeSpan()
    annotate_span(span, info.value)
    assert span.attributes["python_wrap.attempts"] == 2
    assert span.attributes["python_wrap.attempt.1.exception"] == "builtins.OSError"
    assert [event[1]["attempt"] for event in span.events] == [1, 2]
    assert "next_delay" in span.events[0][1] and "next_delay" not in span.events[1][1]
    empty = FakeSpan()
    annotate_span(empty, ValueError())
    assert empty.attributes == {} and empty.events == []
    assert isinstance(attempt_history(info.value)[0], AttemptRecord)
