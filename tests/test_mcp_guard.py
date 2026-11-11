"""mcp_guard: timeouts, selective retries, per-server breakers, cooperative cancel, PW020-21."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from python_wrap import (
    CircuitBreaker,
    CircuitOpenError,
    MCPGuard,
    RunCancelled,
    TimeoutExpired,
    agent_run,
    mcp_breaker,
    mcp_guard,
    retry,
    timeout,
)
from python_wrap.doctor import doctor, explain
from python_wrap.mcp_guard import _BREAKERS


@pytest.fixture(autouse=True)
def fresh_breakers() -> Any:
    _BREAKERS.clear()
    yield
    _BREAKERS.clear()


class FakeSession:
    """The slice of an MCP ``ClientSession`` the guard touches (async ``call_tool``)."""

    name = "files"

    def __init__(self, script: list[Any] | None = None, delay: float = 0.0) -> None:
        self.script = list(script or [])
        self.delay = delay
        self.calls: list[tuple[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return {"content": [{"type": "text", "text": name}], "isError": False}

    async def list_tools(self) -> list[str]:
        return ["read", "write"]


async def test_object_proxy_guards_call_tool_and_forwards_the_rest() -> None:
    session = FakeSession()
    guarded = mcp_guard(session, timeout=1)
    assert isinstance(guarded, MCPGuard)
    assert guarded.server == "files" and guarded.breaker is mcp_breaker("files")
    assert (await guarded.call_tool("read", {"path": "a"}))["content"][0]["text"] == "read"
    assert await guarded.list_tools() == ["read", "write"]
    assert "files" in repr(guarded)


async def test_async_timeout_per_attempt() -> None:
    session = FakeSession(delay=1.0)
    guarded = mcp_guard(session, timeout=0.05, breaker=False)
    started = time.monotonic()
    with pytest.raises(TimeoutExpired):
        await guarded.call_tool("read")
    assert time.monotonic() - started < 0.5


async def test_retry_only_for_listed_tools() -> None:
    session = FakeSession([ConnectionError("x"), ConnectionError("y")])
    guarded = mcp_guard(session, retry=3, retry_tools=["read*"], breaker=False)
    await guarded.call_tool("read_file")
    assert [name for name, _ in session.calls] == ["read_file"] * 3

    writes = FakeSession([ConnectionError("x")])
    guarded = mcp_guard(writes, retry=3, retry_tools=["read*"], breaker=False)
    with pytest.raises(ConnectionError):
        await guarded.call_tool("write_file", {"path": "a"})
    assert len(writes.calls) == 1  # a write is never retried blindly


async def test_retry_tools_predicate_and_custom_decorator() -> None:
    session = FakeSession([ValueError("odd")])
    guarded = mcp_guard(
        session,
        retry=retry(max_attempts=2, delay=0, on=ValueError),
        retry_tools=lambda name: name == "stat",
        breaker=False,
    )
    await guarded.call_tool("stat")
    assert len(session.calls) == 2


async def test_breaker_is_shared_per_server_and_opens() -> None:
    first = FakeSession([ConnectionError()] * 10)
    second = FakeSession()
    a = mcp_guard(first, breaker={"failure_threshold": 2, "recovery_timeout": 60})
    b = mcp_guard(second)  # same server name "files": same breaker
    assert a.breaker is b.breaker
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await a.call_tool("read")
    with pytest.raises(CircuitOpenError):
        await b.call_tool("read")
    assert second.calls == []
    other = mcp_guard(FakeSession(), server="search")
    assert other.breaker is not a.breaker


async def test_error_results_are_answers_not_failures() -> None:
    session = FakeSession([{"isError": True, "content": []}] * 5)
    guarded = mcp_guard(session, breaker={"failure_threshold": 1})
    for _ in range(3):
        assert (await guarded.call_tool("read"))["isError"] is True
    assert guarded.breaker is not None and guarded.breaker.stats.failures == 0


async def test_explicit_breaker_and_disabled() -> None:
    mine = CircuitBreaker(failure_threshold=1, name="mine")
    guarded = mcp_guard(FakeSession([ConnectionError()]), breaker=mine)
    with pytest.raises(ConnectionError):
        await guarded.call_tool("read")
    with pytest.raises(CircuitOpenError):
        await guarded.call_tool("read")
    assert mcp_guard(FakeSession(), breaker=None).breaker is None


async def test_run_cancel_stops_in_flight_async_call() -> None:
    session = FakeSession(delay=5.0)
    guarded = mcp_guard(session, timeout=30, breaker=False)
    async with agent_run() as run:

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            run.cancel()

        canceller = asyncio.create_task(cancel_soon())
        started = time.monotonic()
        with pytest.raises(TimeoutExpired):
            await guarded.call_tool("read")
        await canceller
        assert time.monotonic() - started < 1.0
        with pytest.raises((TimeoutExpired, RunCancelled)):
            await guarded.call_tool("read")
    assert len(session.calls) == 1  # the second call never reached the server


def test_sync_function_target_with_timeout_retry_and_checkpoint() -> None:
    calls: list[str] = []

    def call_tool(name: str, arguments: dict[str, Any] | None = None) -> str:
        calls.append(name)
        if len(calls) == 1:
            raise ConnectionError("restart")
        return "ok"

    guarded = mcp_guard(call_tool, server="calc", retry=2, retry_tools="get_*", timeout=2)
    assert guarded("get_x") == "ok"
    assert calls == ["get_x", "get_x"]
    assert guarded.__name__ == "call_tool"


def test_sync_timeout_fires() -> None:
    def call_tool(name: str) -> str:
        time.sleep(0.5)
        return "late"

    guarded = mcp_guard(call_tool, timeout=0.05, breaker=False)
    result: list[BaseException] = []

    def run() -> None:  # off the main thread: worker-thread timeout
        try:
            guarded("slow")
        except BaseException as exc:
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert isinstance(result[0], TimeoutExpired)


def test_cancelled_token_refuses_before_calling() -> None:
    calls: list[str] = []

    def call_tool(name: str) -> str:
        calls.append(name)
        return "ok"

    guarded = mcp_guard(call_tool, breaker=False, timeout=None)

    @timeout(5)
    def inside() -> None:
        from python_wrap.concurrency import current_token

        token = current_token()
        assert token is not None
        token.cancel()
        guarded("x")

    with pytest.raises(TimeoutExpired):
        inside()
    assert calls == []


def test_argument_validation() -> None:
    with pytest.raises(TypeError):
        mcp_guard(42)
    with pytest.raises(ValueError):
        mcp_guard(lambda name: name, timeout=0)
    with pytest.raises(ValueError):
        mcp_guard(lambda name: name, retry=0)
    with pytest.raises(TypeError):
        mcp_guard(lambda name: name, retry="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        mcp_guard(lambda name: name, breaker="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mcp_breaker("")


def test_doctor_pw020_and_pw021() -> None:
    def call_tool(name: str) -> str:
        return name

    naked = mcp_guard(call_tool, timeout=None, retry=3, breaker=False, server="srv")
    codes = [finding.code for finding in doctor(naked).findings]
    assert "PW020" in codes and "PW021" in codes

    safe = mcp_guard(call_tool, timeout=10, retry=3, retry_tools=["get_*"], server="srv2")
    codes = [finding.code for finding in doctor(safe).findings]
    assert "PW020" not in codes and "PW021" not in codes

    proxy = mcp_guard(FakeSession(), timeout=None)
    assert [f.code for f in explain(proxy).findings] == ["PW020"]


async def test_chaos_flapping_server_breaker_recovers(fake_clock: Any) -> None:
    script: list[Any] = [ConnectionError()] * 3 + [{"ok": 1}] * 3
    session = FakeSession(script)
    guarded = mcp_guard(session, breaker={"failure_threshold": 3, "recovery_timeout": 10})
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await guarded.call_tool("read")
    with pytest.raises(CircuitOpenError):
        await guarded.call_tool("read")
    fake_clock.advance(11)
    assert await guarded.call_tool("read") == {"ok": 1}
    assert await guarded.call_tool("read") == {"ok": 1}
