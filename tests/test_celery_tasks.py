"""resilient_task, idempotent_task and the hard-limit deadline for Celery tasks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from python_wrap.celery import check_visibility, idempotent_task, resilient_task
from python_wrap.circuit import CircuitBreaker
from python_wrap.concurrency import RateLimiter
from python_wrap.errors import CircuitOpenError


class Requeued(Exception):
    pass


class Task:
    name = "demo.task"

    def __init__(self) -> None:
        self.request = SimpleNamespace(retries=0)
        self.countdown: float | None = None

    def retry(self, **options: Any) -> Requeued:
        self.countdown = options["countdown"]
        return Requeued()


def test_idempotent_task_runs_body_once_for_a_redelivery() -> None:
    runs: list[int] = []

    @idempotent_task()
    def body(self: Task, n: int) -> int:
        runs.append(n)
        return n * 2

    task = Task()
    assert body(task, 3) == body(task, 3) == 6
    assert runs == [3]
    assert body(task, 4) == 8


def test_resilient_task_requeues_with_the_breakers_retry_after() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=42, name="g12-vendor")

    @resilient_task(breaker)
    def body(self: Task) -> None:
        raise ConnectionError

    task = Task()
    with pytest.raises(ConnectionError):
        body(task)  # opens the breaker; ConnectionError is not a requeue error
    with pytest.raises(Requeued):
        body(task)
    assert task.countdown is not None and 40 < task.countdown <= 42


def test_blocking_limiter_requeues_instead_of_sleeping() -> None:
    limiter = RateLimiter(1, 60, block=True)

    @resilient_task(limiter)
    def body(self: Task) -> int:
        return 1

    task = Task()
    assert body(task) == 1
    with pytest.raises(Requeued):
        body(task)
    assert task.countdown is not None and task.countdown > 50


def test_unbound_task_is_rejected() -> None:
    @resilient_task()
    def body(x: int) -> int:
        return x

    with pytest.raises(TypeError, match="bind=True"):
        body(1)


def test_visibility_check_flags_blocking_limiter() -> None:
    limiter = RateLimiter(1, 1, block=True)
    assert [f.code for f in check_visibility(limiter(lambda: 1), 300)] == ["PW023"]
    assert check_visibility(CircuitBreaker(name="g12-ok")(lambda: 1), 300) == ()


def test_error_type_is_circuit_open() -> None:
    assert issubclass(CircuitOpenError, Exception)
