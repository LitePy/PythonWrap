"""Celery-aware retry countdowns and shared breakers, tested without Celery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from python_wrap import CircuitBreaker, CircuitOpenError, RateLimitExceeded, RedisBreakerState
from python_wrap.celery import retry_countdown, retry_task, shared_breaker


class Retry(Exception):
    """Stands in for celery.exceptions.Retry."""


class FakeTask:
    def __init__(self, retries: int = 0, max_retries: int = 3) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.max_retries = max_retries
        self.calls: list[dict[str, object]] = []

    def retry(self, **options: object) -> BaseException:
        self.calls.append(options)
        limit = options.get("max_retries", self.max_retries)
        exc = options["exc"]
        if self.request.retries >= limit and isinstance(exc, BaseException):
            return exc
        return Retry()


@pytest.mark.parametrize(("retries", "expected"), [(0, 5), (1, 10), (2, 20), (3, 40), (9, 600)])
def test_exponential_schedule_by_celery_retry_count(retries, expected):
    assert retry_countdown(FakeTask(retries)) == expected


def test_explicit_delays_repeat_the_last_entry():
    waits = [retry_countdown(FakeTask(n), delays=[10, 60, 300]) for n in range(5)]
    assert waits == [10, 60, 300, 300, 300]


def test_waits_requested_by_the_failure_win_and_are_capped():
    task = FakeTask(0)
    assert retry_countdown(task, CircuitOpenError("open", retry_after=42)) == 42
    assert retry_countdown(task, RateLimitExceeded("slow", retry_after=7.5)) == 7.5
    response = SimpleNamespace(headers={"Retry-After": "120"})
    http_error = RuntimeError("429")
    http_error.response = response  # type: ignore[attr-defined]
    assert retry_countdown(task, http_error) == 120
    assert retry_countdown(task, http_error, max_delay=60) == 60
    assert retry_countdown(task, http_error, respect_retry_after=False) == 5


def test_jitter_is_bounded():
    for _ in range(50):
        wait = retry_countdown(FakeTask(0), delay=1, jitter=0.5)
        assert 1 <= wait <= 1.5


def test_task_without_request_counts_as_first_retry():
    assert retry_countdown(object()) == 5


@pytest.mark.parametrize(
    "options",
    [{"delay": -1}, {"backoff": 0.5}, {"jitter": -1}, {"max_delay": -1}, {"delays": []}],
)
def test_invalid_options(options):
    with pytest.raises(ValueError):
        retry_countdown(FakeTask(), **options)


def test_retry_task_raises_celery_retry_with_countdown():
    task = FakeTask(1)
    error = ConnectionError("down")
    with pytest.raises(Retry):
        retry_task(task, error, delays=[30, 120])
    assert task.calls == [{"exc": error, "countdown": 120}]


def test_retry_task_reraises_once_exhausted_and_passes_max_retries():
    task = FakeTask(retries=2)
    error = ConnectionError("down")
    with pytest.raises(ConnectionError):
        retry_task(task, error, max_retries=2)
    assert task.calls[0]["max_retries"] == 2


class FakeRedis:
    """Records the eval call RedisBreakerState makes and answers 'closed'."""

    def __init__(self) -> None:
        self.calls = 0

    def eval(self, *args: object) -> list[object]:
        self.calls += 1
        return [1, b"closed", 0, 0, 0, 0, 0, 0, 0, 0, b"closed"]


def test_shared_breaker_wraps_a_redis_client():
    client = FakeRedis()
    breaker = shared_breaker("vendor-a", client, failure_threshold=3)
    assert isinstance(breaker, CircuitBreaker)
    assert isinstance(breaker.backend, RedisBreakerState)
    assert breaker.name == "vendor-a" and breaker.failure_threshold == 3

    @breaker
    def call() -> str:
        return "ok"

    assert call() == "ok"
    assert client.calls >= 1


def test_shared_breaker_accepts_a_state_backend_and_needs_a_name():
    state = RedisBreakerState(FakeRedis())
    assert shared_breaker("vendor-b", state).backend is state
    with pytest.raises(ValueError):
        shared_breaker("", state)
