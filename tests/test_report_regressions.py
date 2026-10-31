"""Regression tests for reported package issues."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from python_wrap import _core, http_status, is_sensitive, memoize, redact, retry, retry_after_header
from python_wrap.testing import fake_clock


def test_p1_public_redaction_does_not_mask_domain_names():
    for name in [
        "author",
        "author_id",
        "authorised_by",
        "session_id",
        "session_date",
        "study_session",
    ]:
        assert not is_sensitive(name)
    for name in ["password", "password2", "apiKey", "HTTPApiKey", "session_token", "authSecret"]:
        assert is_sensitive(name)
    value = {"author": "ana", "rows": [{"password": "do-not-log"}]}
    assert redact(value) == {"author": "ana", "rows": [{"password": "***"}]}
    assert value["rows"][0]["password"] == "do-not-log"


def test_p2_header_pairs_and_none_results_are_supported():
    response = SimpleNamespace(headers=[("retry-after", "3")], status_code=429)
    assert retry_after_header(response) == 3
    with fake_clock() as clock:

        @retry(max_attempts=2, retry_if_result=http_status(429), delay_from=retry_after_header)
        def work():
            return response

        assert work() is response
        assert clock.sleeps == [3]


@pytest.mark.asyncio
async def test_p3_cancellation_from_hook_is_never_swallowed():
    async def hook(state):
        raise asyncio.CancelledError()

    @retry(max_attempts=3, delay=0, on_retry=hook)
    async def work():
        raise ValueError("original")

    with pytest.raises(asyncio.CancelledError):
        await work()


def test_p5_external_deadline_tracks_wall_clock_step(monkeypatch):
    class Store:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def set(self, key, entry, ttl):
            self.data[key] = entry

        def delete(self, key):
            return self.data.pop(key, None) is not None

        def clear(self):
            self.data.clear()

        def __len__(self):
            return len(self.data)

    wall = [1000.0]
    monkeypatch.setattr(_core, "wall_time", lambda: wall[0])
    monkeypatch.setattr(_core, "monotonic", lambda: 100.0)
    calls = []

    @memoize(store=Store(), ttl=10)
    def work():
        calls.append(1)
        return len(calls)

    assert work() == work() == 1
    wall[0] = 1011
    assert work() == 2


@pytest.mark.asyncio
async def test_async_json_retry_hint_does_not_leave_unawaited_coroutine():
    class Response:
        async def json(self):
            return {"retry_after": 1}

    assert retry_after_header(Response(), json_key="retry_after") is None


def test_failing_retry_hook_logger_cannot_replace_original_outcome():
    import logging

    class BrokenLogger(logging.Logger):
        def exception(self, *args, **kwargs):
            raise RuntimeError("handler failed")

    def hook(state):
        raise RuntimeError("hook failed")

    @retry(max_attempts=2, delay=0, on_retry=hook, logger=BrokenLogger("broken"), log_level=None)
    def work():
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        work()
