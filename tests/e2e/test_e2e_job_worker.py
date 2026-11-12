"""Story: a job worker ignores duplicate deliveries and loses no results across a restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from python_wrap import idempotent
from python_wrap.buffering import Buffered, SQLiteJournal

pytestmark = pytest.mark.e2e


def test_duplicate_deliveries_run_the_job_once() -> None:
    sent: list[str] = []

    @idempotent(key=lambda job_id, to: job_id, ttl="1h")
    def send_welcome_email(job_id: str, to: str) -> str:
        sent.append(to)
        return f"sent-{job_id}"

    assert send_welcome_email("job-1", "ana@example.com") == "sent-job-1"
    assert send_welcome_email("job-1", "ana@example.com") == "sent-job-1"  # redelivered
    assert send_welcome_email("job-2", "ben@example.com") == "sent-job-2"
    assert sent == ["ana@example.com", "ben@example.com"]


def test_failed_job_can_be_retried_and_is_then_remembered() -> None:
    attempts: list[str] = []

    @idempotent(key=lambda job_id: job_id)
    def flaky(job_id: str) -> str:
        attempts.append(job_id)
        if len(attempts) == 1:
            raise ConnectionError("broker hiccup")
        return "done"

    with pytest.raises(ConnectionError):
        flaky("j")
    assert flaky("j") == "done"
    assert flaky("j") == "done"
    assert attempts == ["j", "j"]


def test_results_survive_a_destination_outage_and_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "outbox.db")

    def unavailable(rows: list[dict[str, Any]]) -> None:
        raise ConnectionError("warehouse down")

    first = Buffered(unavailable, flush_interval=0.01, journal=SQLiteJournal(path))
    for n in range(3):
        assert first.submit({"job": n, "status": "done"})
    first.close(timeout=2)  # the process "dies" with nothing delivered
    first.journal.close()

    reopened = SQLiteJournal(path)
    assert len(reopened.pending()) == 3
    reopened.close()

    delivered: list[dict[str, Any]] = []
    journal = SQLiteJournal(path)
    second = Buffered(delivered.extend, flush_interval=0.01, journal=journal)
    assert second.replay() == 3
    assert second.close(timeout=2)

    assert sorted(row["job"] for row in delivered) == [0, 1, 2]
    assert journal.pending() == []
    journal.close()
