from __future__ import annotations

import asyncio
import functools
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


def test_audit_survives_broken_exception_repr() -> None:
    from python_wrap.logs import audit

    class Weird(Exception):
        def __repr__(self) -> str:
            raise RuntimeError("repr broke")

    records: list[Any] = []

    @audit(sink=records.append)
    def f() -> None:
        raise Weird("real error")

    with pytest.raises(Weird, match="real error"):
        f()
    assert records[0].status == "error"
    assert "unrepresentable Weird" in records[0].error


def test_failing_finish_hook_never_replaces_result_or_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:

    from python_wrap.logs import _instrument

    def bad_finish(state: None, ok: bool, value: Any) -> None:
        raise RuntimeError("hook broke")

    @functools.partial(_instrument, start=lambda a, k: None, finish=bad_finish)
    def ok() -> int:
        return 5

    @functools.partial(_instrument, start=lambda a, k: None, finish=bad_finish)
    def bad() -> int:
        raise KeyError("orig")

    @functools.partial(_instrument, start=lambda a, k: None, finish=bad_finish)
    async def aok() -> int:
        return 6

    with caplog.at_level(logging.ERROR):
        assert ok() == 5
        with pytest.raises(KeyError):
            bad()
        assert asyncio.run(aok()) == 6
    assert sum("hook failed" in r.getMessage() for r in caplog.records) == 3


# ------------------------------------------------------------------------- log_context


def test_log_context_adds_fields_to_every_decorator(caplog: pytest.LogCaptureFixture) -> None:
    from python_wrap import timing
    from python_wrap.logs import current_log_context, log_context

    caplog.set_level(logging.DEBUG, logger="python_wrap")
    records: list[AuditRecord] = []

    @log_args
    @log_return
    @audit(sink=records.append)
    @trace(limit=1)
    @timing
    def f(x: int) -> int:
        return x

    assert current_log_context() == {}
    f(1)
    assert not any("request_id" in m for m in messages(caplog))
    assert records[0].context == {}
    caplog.clear()
    with log_context(request_id="r%1") as fields:
        assert fields == {"request_id": "r%1"}
        with log_context(user="ana"):
            assert current_log_context() == {"request_id": "r%1", "user": "ana"}
            f(2)
        assert current_log_context() == {"request_id": "r%1"}
    assert current_log_context() == {}
    assert records[1].context == {"request_id": "r%1", "user": "ana"}
    assert caplog.records
    for r in caplog.records:
        assert r.context == {"request_id": "r%1", "user": "ana"}  # type: ignore[attr-defined]
    suffix = "[request_id=r%1 user=ana]"
    assert next(m for m in messages(caplog) if m.startswith("Calling")).endswith(suffix)
    assert any(".f took" in m and m.endswith(suffix) for m in messages(caplog))


def test_log_context_decorator_and_async(caplog: pytest.LogCaptureFixture) -> None:
    from python_wrap.logs import current_log_context, log_context

    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_context(job="nightly")
    def sync() -> dict[str, object]:
        return current_log_context()

    @log_context(job="async")
    async def coro() -> dict[str, object]:
        await asyncio.sleep(0)
        return current_log_context()

    assert inspect.iscoroutinefunction(coro)
    assert sync() == {"job": "nightly"}
    assert asyncio.run(coro()) == {"job": "async"}
    assert current_log_context() == {}

    @log_return
    async def work() -> int:
        return 1

    async def main() -> None:
        async with log_context(tenant="t1"):
            await work()

    asyncio.run(main())
    assert messages(caplog)[-1].endswith("work returned 1 [tenant=t1]")


def test_log_context_is_isolated_per_task_and_thread() -> None:
    import contextvars
    import threading

    from python_wrap.logs import current_log_context, log_context

    async def worker(n: int) -> dict[str, object]:
        with log_context(n=n):
            await asyncio.sleep(0)
            return current_log_context()

    async def main() -> list[dict[str, object]]:
        return list(await asyncio.gather(*(worker(i) for i in range(3))))

    assert asyncio.run(main()) == [{"n": 0}, {"n": 1}, {"n": 2}]

    seen: dict[str, dict[str, object]] = {}
    with log_context(a=1):
        t = threading.Thread(target=lambda: seen.update(plain=current_log_context()))
        t.start()
        t.join()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.update(copied=ctx.run(current_log_context)))
        t.start()
        t.join()
    assert seen == {"plain": {}, "copied": {"a": 1}}


def test_log_context_restores_on_error() -> None:
    from python_wrap.logs import current_log_context, log_context

    with pytest.raises(RuntimeError), log_context(a=1):
        raise RuntimeError
    assert current_log_context() == {}


def test_log_context_unprintable_value(caplog: pytest.LogCaptureFixture) -> None:
    from python_wrap.logs import log_context

    class Bad:
        def __str__(self) -> str:
            raise RuntimeError

    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_return
    def f() -> int:
        return 1

    with log_context(bad=Bad()):
        f()
    assert "<unprintable Bad>" in messages(caplog)[-1]


# --------------------------------------------------------- fast paths (positional calls)


def _calls(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m.split("Calling ", 1)[1].split(".")[-1] for m in messages(caplog)]


def test_log_args_fast_path_matches_bind_for_every_signature_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args
    def plain(a: int, b: int = 2, password: str = "x") -> None: ...

    @log_args
    def pos_only(a: int, token: str, /, c: int = 3) -> None: ...

    @log_args
    def kw_only(a: int, *, secret: str = "s", n: int = 1) -> None: ...

    @log_args
    def variadic(a: int, *rest: int, api_key: str = "k", **extra: int) -> None: ...

    plain(1)
    plain(1, 5, "pw")
    plain(1, b=9)
    pos_only(1, "tok")
    pos_only(1, "tok", c=7)
    kw_only(1)
    kw_only(1, secret="zz", n=4)
    variadic(1, 2, 3, x=4)
    variadic(1, api_key="q", y=5)

    assert _calls(caplog) == [
        "plain(a=1)",
        "plain(a=1, b=5, password='***')",
        "plain(a=1, b=9)",
        "pos_only(a=1, token='***')",
        "pos_only(a=1, token='***', c=7)",
        "kw_only(a=1)",
        "kw_only(a=1, secret='***', n=4)",
        "variadic(a=1, rest=(2, 3), x=4)",
        "variadic(a=1, api_key='***', y=5)",
    ]
    assert "pw" not in caplog.text.replace("password", "")


def test_log_args_redacts_var_kwargs_names_not_in_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args
    def f(**extra: str) -> None: ...

    f(my_token="abc", plain="ok")
    assert "abc" not in caplog.text
    assert "my_token='***'" in caplog.text
    assert "plain='ok'" in caplog.text


def test_log_args_too_many_positionals_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    @log_args
    def f(a: int) -> int:
        return a

    with pytest.raises(TypeError):
        f(1, 2)  # type: ignore[call-arg]
    assert "f(arg0=1, arg1=2)" in caplog.text


def test_log_args_methods_name_self_and_redact(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="python_wrap")

    class Svc:
        @log_args
        def login(self, user: str, password: str) -> None: ...

        @classmethod
        @log_args
        def make(cls, name: str, token: str) -> None: ...

        @staticmethod
        @log_args
        def util(x: int, auth: str) -> None: ...

    Svc().login("u", "pw1")
    Svc.make("n", "tk1")
    Svc.util(1, "au1")
    for secret in ("pw1", "tk1", "au1"):
        assert secret not in caplog.text
    assert "user='u', password='***'" in caplog.text
    assert "name='n', token='***'" in caplog.text
    assert "x=1, auth='***'" in caplog.text


def test_audit_records_arguments_on_fast_and_slow_paths() -> None:
    records: list[AuditRecord] = []

    @audit(sink=records.append)
    def f(a: int, password: str = "d", *, token: str = "t", n: int = 0) -> None: ...

    f(1)
    f(1, "pw")
    f(1, n=2)
    f(a=3, token="x")
    assert [r.arguments for r in records] == [
        {"a": 1},
        {"a": 1, "password": "***"},
        {"a": 1, "n": 2},
        {"a": 3, "token": "***"},
    ]


def test_audit_caller_location_stays_exact_with_cache() -> None:
    records: list[AuditRecord] = []

    @audit(sink=records.append)
    def f(x: int) -> int:
        return x

    for _ in range(3):  # later calls hit the filename cache
        frame = inspect.currentframe()
        assert frame is not None
        line = frame.f_lineno + 1
        f(1)
        assert records[-1].caller == f"{__file__}:{line}"


def test_audit_with_sink_but_level_disabled_still_builds_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="python_wrap")
    records: list[AuditRecord] = []

    @audit(sink=records.append, level=logging.INFO, include_result=True)
    def f(a: int, secret: str) -> int:
        return a

    assert f(1, "s3") == 1
    assert len(records) == 1
    assert records[0].arguments == {"a": 1, "secret": "***"}
    assert records[0].result == 1
    assert records[0].caller.startswith(__file__)
    assert caplog.records == []


def test_audit_without_sink_and_level_disabled_skips_record(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from python_wrap import logs

    caplog.set_level(logging.ERROR, logger="python_wrap")
    built: list[object] = []
    monkeypatch.setattr(logs, "AuditRecord", lambda **kw: built.append(kw))

    @audit
    def f(a: int) -> int:
        return a

    @audit
    def boom() -> None:
        raise ZeroDivisionError

    assert f(1) == 1
    with pytest.raises(ZeroDivisionError):
        boom()
    assert built == []
    assert caplog.records == []


def test_audit_async_with_sink_and_level_disabled() -> None:
    records: list[AuditRecord] = []

    @audit(sink=records.append, logger="pw.off", level=logging.DEBUG)
    async def f(a: int, token: str) -> int:
        return a

    assert asyncio.run(f(2, "t")) == 2
    assert records[0].arguments == {"a": 2, "token": "***"}
