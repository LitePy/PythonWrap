from __future__ import annotations

import logging

import pytest

from conftest import FakeClock
from python_wrap import retry, retry_stats, retrying

SECRET = "https://example.test?api_key=private-value"


class PrivateResult:
    def __repr__(self) -> str:
        raise AssertionError("must not inspect response contents")


@pytest.mark.parametrize("inline", [False, True])
def test_type_only_exception_logs_preserve_error(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture, inline: bool
) -> None:
    original = ConnectionError(SECRET)

    def job() -> None:
        raise original

    def hint(value: object) -> None:
        raise ValueError(SECRET)

    def hook(value: object) -> None:
        raise RuntimeError(SECRET)

    options = {
        "max_attempts": 2,
        "delay": 0,
        "delay_from": hint,
        "on_retry": hook,
        "log_values": False,
        "log_exc_info": True,
        "logger": "private-retry",
    }
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectionError) as caught:
        if inline:
            for attempt in retrying(**options):
                with attempt:
                    job()
        else:
            wrapped = retry(job, **options)
            wrapped()
    assert caught.value is original
    assert SECRET not in caplog.text
    assert "ConnectionError" in caplog.text
    assert "ValueError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None or record.exc_info is False for record in caplog.records)


async def test_type_only_result_and_async_hook_logs(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    result = PrivateResult()

    async def hook(value: object) -> None:
        raise RuntimeError(SECRET)

    @retry(
        max_attempts=2,
        delay=0,
        retry_if_result=lambda value: True,
        on_retry=hook,
        log_values=False,
        logger="private-retry",
    )
    async def job() -> PrivateResult:
        return result

    assert await job() is result
    assert "returned PrivateResult" in caplog.text
    assert SECRET not in caplog.text
    stats = retry_stats(job)
    assert stats is not None and stats.snapshot()["attempts_total"] == 2
