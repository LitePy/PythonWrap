from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import timedelta, timezone
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.logs import DEFAULT_REDACT, AuditRecord, audit, log_args, log_return, trace


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------- log_args


def test_log_args_names_positionals_and_redacts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args
    def login(user: str, password: str, *, retries: int = 3) -> str:
        return user

    assert login("bob", "hunter2", retries=2) == "bob"
    assert caplog.records[0].levelno == logging.DEBUG
    assert messages(caplog)[0] == "Calling login(user='bob', password='***', retries=2)" or (
        messages(caplog)[0].endswith("login(user='bob', password='***', retries=2)")
    )
    assert "hunter2" not in caplog.text


def test_log_args_case_insensitive_substring_and_kwargs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args
    def f(*args: Any, **kwargs: Any) -> None: ...

    f(1, API_KEY="abc", user_token="t", name="n")
    msg = messages(caplog)[0]
    assert "API_KEY='***'" in msg
    assert "user_token='***'" in msg
    assert "name='n'" in msg
    assert "args=(1,)" in msg


def test_log_args_custom_redact_and_disable(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args(redact=["pin"])
    def a(pin: str, password: str) -> None: ...

    @log_args(redact=())
    def b(password: str) -> None: ...

    a("1234", "pw")
    b("pw")
    assert "pin='***'" in messages(caplog)[0]
    assert "password='pw'" in messages(caplog)[0]
    assert "password='pw'" in messages(caplog)[1]
    assert "password" in DEFAULT_REDACT


def test_log_args_truncates_and_level(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @log_args(level=logging.INFO, max_length=30)
    def f(blob: str) -> None: ...

    f("x" * 500)
    assert caplog.records[0].levelno == logging.INFO
    assert len(messages(caplog)[0]) < 100
    assert "..." in messages(caplog)[0]


def test_log_args_default_level_is_debug_and_skipped_when_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @log_args
    def f(x: int) -> int:
        return x

    assert f(1) == 1
    assert caplog.records == []


def test_log_args_bad_call_and_bad_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    class Bad:
        def __repr__(self) -> str:
            raise RuntimeError

    @log_args
    def f(x: object) -> None: ...

    with pytest.raises(TypeError):
        f(1, 2)  # type: ignore[call-arg]
    f(Bad())
    assert "arg0=1" in messages(caplog)[0]
    assert "Bad instance" in messages(caplog)[1]


def test_log_args_async_and_methods(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    class C:
        @log_args
        async def a(self, secret: str) -> int:
            return 1

        @staticmethod
        @log_args
        def s(x: int) -> int:
            return x

    assert inspect.iscoroutinefunction(C.a)
    assert asyncio.run(C().a("s")) == 1
    assert C.s(2) == 2
    assert "secret='***'" in messages(caplog)[0]
    assert "s(x=2)" in messages(caplog)[1]


# -------------------------------------------------------------------------- log_return


def test_log_return_value_and_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_return
    def f(fail: bool) -> list[int]:
        if fail:
            raise ValueError("nope")
        return [1, 2]

    assert f(False) == [1, 2]
    with pytest.raises(ValueError, match="nope"):
        f(True)
    assert messages(caplog)[0].endswith("returned [1, 2]")
    assert messages(caplog)[1].endswith("raised ValueError")


def test_log_return_truncation_and_async(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="r")

    @log_return(max_length=20, level=logging.INFO, logger="r")
    async def f() -> str:
        return "y" * 100

    assert inspect.iscoroutinefunction(f)
    asyncio.run(f())

    @log_return(logger="r")
    async def g() -> None:
        raise KeyError

    with pytest.raises(KeyError):
        asyncio.run(g())
    assert caplog.records[0].name == "r"
    assert caplog.records[0].levelno == logging.INFO
    assert len(messages(caplog)[0]) < 100
    assert messages(caplog)[1].endswith("raised KeyError")


# ------------------------------------------------------------------------------ audit


def test_audit_record_fields_and_real_caller(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")
    records: list[AuditRecord] = []

    @audit(sink=records.append, include_result=True)
    def transfer(amount: int, api_key: str) -> str:
        fake_clock.advance(0.5)
        return "done"

    frame = inspect.currentframe()
    assert frame is not None
    call_line = frame.f_lineno + 1
    assert transfer(10, "k-123") == "done"
    rec = records[0]
    assert rec.function.endswith("transfer")
    assert rec.arguments == {"amount": 10, "api_key": "***"}
    assert rec.status == "success"
    assert rec.error is None
    assert rec.result == "done"
    assert rec.duration == pytest.approx(0.5)
    assert rec.timestamp.utcoffset() == timedelta(0)
    assert rec.timestamp.tzinfo == timezone.utc
    # the caller is this test, not the python_wrap wrapper
    assert rec.caller == f"{__file__}:{call_line}"
    assert caplog.records[0].audit is rec  # type: ignore[attr-defined]
    assert "k-123" not in caplog.text


def test_audit_error_status_and_no_result_by_default(fake_clock: FakeClock) -> None:
    records: list[AuditRecord] = []

    @audit(sink=records.append)
    def f(x: int) -> int:
        if x:
            raise ValueError("bad")
        return 5

    with pytest.raises(ValueError):
        f(1)
    f(0)
    assert records[0].status == "error"
    assert records[0].error == "ValueError('bad')"
    assert records[1].result is None


def test_audit_record_is_frozen() -> None:
    records: list[AuditRecord] = []

    @audit(sink=records.append)
    def f() -> None: ...

    f()
    with pytest.raises(AttributeError):
        records[0].status = "x"  # type: ignore[misc]


def test_audit_sink_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    def bad(record: AuditRecord) -> None:
        raise RuntimeError

    @audit(sink=bad)
    def f() -> int:
        return 1

    assert f() == 1
    assert any("sink" in m for m in messages(caplog))


def test_audit_bare_and_async_caller(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @audit
    async def f(session_id: str) -> int:
        return 1

    async def main() -> int:
        return await f("abc")

    assert inspect.iscoroutinefunction(f)
    assert asyncio.run(main()) == 1
    record: AuditRecord = caplog.records[0].audit  # type: ignore[attr-defined]
    assert record.arguments == {"session_id": "***"}
    assert record.caller.startswith(__file__)


def test_audit_caller_skips_stacked_wrappers() -> None:
    records: list[AuditRecord] = []

    @log_return
    @audit(sink=records.append)
    def f() -> None: ...

    f()
    assert records[0].caller.startswith(__file__)


# ------------------------------------------------------------------------------ trace


def outer_frame_fn(fn: Any) -> None:
    fn()


def test_trace_logs_entry_stack_exit(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @trace
    def target() -> int:
        return 1

    outer_frame_fn(target)
    msgs = messages(caplog)
    assert msgs[0].startswith("Entering ") and msgs[0].endswith("target")
    assert "outer_frame_fn" in msgs[1]
    assert "test_trace_logs_entry_stack_exit" in msgs[1]
    assert "python_wrap/logs.py" not in msgs[1]
    assert msgs[2].startswith("Exiting ") and msgs[2].endswith("target")


def test_trace_limit_and_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @trace(limit=1)
    def target() -> None:
        raise ValueError

    with pytest.raises(ValueError):
        outer_frame_fn(target)
    stack = messages(caplog)[1]
    assert "outer_frame_fn" in stack
    assert stack.count('File "') == 1
    assert messages(caplog)[2].endswith("target (raised ValueError)")


def test_trace_limit_zero_and_disabled(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @trace(limit=0)
    def target() -> None: ...

    target()
    assert messages(caplog)[1].strip().endswith(":")
    caplog.clear()
    caplog.set_level(logging.INFO, logger="python_wrap")
    target()
    assert caplog.records == []


def test_trace_async(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @trace(level=logging.INFO)
    async def target() -> int:
        await _core.async_sleep(0)
        return 4

    assert inspect.iscoroutinefunction(target)
    assert asyncio.run(target()) == 4
    assert len(caplog.records) == 3
    assert caplog.records[0].levelno == logging.INFO
