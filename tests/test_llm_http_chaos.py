"""HTTP-level chaos with duck-typed transports (no httpx required) and the pytest plugin."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import python_wrap
from python_wrap import _core
from python_wrap.chaos import FaultInjector, chaos
from python_wrap.llm_cassette import build_response
from python_wrap.llm_http_chaos import ChaosTransport, LLMHTTPFaults


class Req:
    method, url, headers, content = "POST", "https://llm.example/v1", {}, b"{}"


class Wire:
    def __init__(self, body: bytes = b'{"text": "hello world"}') -> None:
        self.calls = 0
        self.body = body

    def handle_request(self, request: Any) -> Any:
        self.calls += 1
        return build_response(
            request, 200, [("content-type", "application/json")], [self.body], asynchronous=False
        )

    async def handle_async_request(self, request: Any) -> Any:
        self.calls += 1
        return build_response(request, 200, [], [self.body], asynchronous=True)


@pytest.fixture
def faults() -> LLMHTTPFaults:
    return LLMHTTPFaults(FaultInjector())


def test_no_rules_means_pass_through(faults: LLMHTTPFaults) -> None:
    wire = Wire()
    assert faults.transport(wire).handle_request(Req()).status_code == 200
    assert wire.calls == 1


def test_error_responses_do_not_reach_the_network(faults: LLMHTTPFaults) -> None:
    wire = Wire()
    transport = faults.transport(wire)
    with faults.rate_limit(retry_after=2.5, times=1):
        response = transport.handle_request(Req())
    assert response.status_code == 429 and response.headers["retry-after"] == "2.5"
    assert json.loads(response.read())["error"]["type"] == "rate_limit_error"
    with faults.overloaded(times=1):
        assert transport.handle_request(Req()).status_code == 529
    with faults.server_error(status=502, times=1):
        assert transport.handle_request(Req()).status_code == 502
    with faults.rate_limit(retry_after=None, times=1):
        assert "retry-after" not in transport.handle_request(Req()).headers
    assert wire.calls == 0


def test_timeout_raises_a_timeout_error(
    faults: LLMHTTPFaults, monkeypatch: pytest.MonkeyPatch
) -> None:
    import python_wrap.llm_http_chaos as mod

    monkeypatch.setattr(mod, "_httpx", lambda: None)
    with faults.timeout(times=1), pytest.raises(TimeoutError):
        faults.transport(Wire()).handle_request(Req())


def test_stall_uses_the_clock(faults: LLMHTTPFaults, fake_clock: Any) -> None:
    with faults.stall(seconds=12, times=1):
        assert faults.transport(Wire()).handle_request(Req()).status_code == 200
    assert fake_clock.sleeps == [12.0]


def test_truncate_cuts_the_body(faults: LLMHTTPFaults) -> None:
    wire = Wire(b"0123456789")
    with faults.truncate(keep=0.4, times=1):
        assert faults.transport(wire).handle_request(Req()).read() == b"0123"
    assert faults.transport(wire).handle_request(Req()).read() == b"0123456789"
    with faults.truncate(keep=0.0, times=1):
        assert faults.transport(wire).handle_request(Req()).read() == b""


def test_rate_and_times_and_point_matching(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(Wire(), point="llm.http.alpha")
    other = faults.transport(Wire(), point="llm.http.beta")
    with faults.rate_limit("llm.http.alpha", times=2):
        assert transport.handle_request(Req()).status_code == 429
        assert other.handle_request(Req()).status_code == 200
        assert transport.handle_request(Req()).status_code == 429
        assert transport.handle_request(Req()).status_code == 200


def test_disabled_injector_injects_nothing() -> None:
    injector = FaultInjector()
    faults = LLMHTTPFaults(injector)
    injector.enabled = False
    with faults.rate_limit():
        assert faults.transport(Wire()).handle_request(Req()).status_code == 200


def test_events_report_injections(faults: LLMHTTPFaults) -> None:
    seen: list[Any] = []
    _core.listeners.append(seen.append)
    try:
        with faults.overloaded(times=1):
            faults.transport(Wire()).handle_request(Req())
    finally:
        _core.listeners.pop()
    assert [e.name for e in seen] == ["chaos.injected"]


def test_async_paths(faults: LLMHTTPFaults, fake_clock: Any) -> None:
    transport = faults.transport(Wire(b"abcdefgh"))

    async def main() -> list[Any]:
        out: list[Any] = []
        with faults.rate_limit(times=1):
            out.append((await transport.handle_async_request(Req())).status_code)
        with faults.stall(seconds=5, times=1):
            out.append((await transport.handle_async_request(Req())).status_code)
        with faults.truncate(keep=0.5, times=1):
            response = await transport.handle_async_request(Req())
            out.append(b"".join([c async for c in response.stream]))
        return out

    assert asyncio.run(main()) == [429, 200, b"abcd"]
    assert fake_clock.sleeps == [5.0]


def test_stream_stall_after_chunks(faults: LLMHTTPFaults, fake_clock: Any) -> None:
    class Chunky:
        def handle_request(self, request: Any) -> Any:
            return build_response(request, 200, [], [b"a", b"b", b"c"], asynchronous=False)

    with faults.stall(seconds=7, after_chunks=1, times=1):
        response = faults.transport(Chunky()).handle_request(Req())
        assert list(response.stream) == [b"a", b"b", b"c"]
    assert fake_clock.sleeps == [7.0]


@pytest.mark.parametrize(
    ("call", "kwargs"),
    [
        ("truncate", {"keep": 1.0}),
        ("truncate", {"keep": -0.1}),
        ("stall", {"seconds": -1}),
        ("stall", {"after_chunks": -1}),
    ],
)
def test_bad_arguments(faults: LLMHTTPFaults, call: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        getattr(faults, call)(**kwargs)


def test_global_chaos_exposes_llm_http() -> None:
    assert isinstance(chaos.llm_http, LLMHTTPFaults)
    assert chaos.llm_http is chaos.llm_http
    assert isinstance(chaos.llm_http.transport(Wire()), ChaosTransport)


def test_context_manager_and_close_forward_to_inner(faults: LLMHTTPFaults) -> None:
    class Closable(Wire):
        closed = entered = False

        def __enter__(self) -> Closable:
            self.entered = True
            return self

        def __exit__(self, *exc: Any) -> None:
            self.closed = True

    inner = Closable()
    with faults.transport(inner):
        assert inner.entered
    assert inner.closed


# ------------------------------------------------------------------------- pytest plugin


CONFTEST = 'pytest_plugins = ["python_wrap.pytest_plugin"]\n'

PLUGIN_TEST = """
from python_wrap.llm_cassette import build_response


class Req:
    method, url = "POST", "https://llm.example/v1"
    headers = {"authorization": "Bearer sk-abcdefghijklmnopqrstuv"}
    content = b'{"q": 1}'


class Wire:
    def handle_request(self, request):
        return build_response(request, 200, [], [b'{"ok": true}'], asynchronous=False)


def test_flow(llm_cassette):
    cassette = llm_cassette()
    response = cassette.transport(Wire()).handle_request(Req())
    assert b"".join(response.stream) == b'{"ok": true}'


def test_named(llm_cassette):
    assert llm_cassette("shared name!", mode="once").path.endswith("shared_name.json")


def test_chaos(chaos_llm_http):
    class W:
        def handle_request(self, request):
            raise AssertionError("never reached")

    with chaos_llm_http.rate_limit(times=1):
        assert chaos_llm_http.transport(W()).handle_request(Req()).status_code == 429
"""


def run_pytest(
    folder: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    src = str(Path(python_wrap.__file__).resolve().parent.parent)
    environment = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    environment.update({"PYTHONPATH": src, **(env or {})})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=folder,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_wire.py").write_text(PLUGIN_TEST)
    return tmp_path


def test_plugin_records_then_replays_without_leaking_secrets(project: Path) -> None:
    first = run_pytest(project)
    assert "3 passed" in first.stdout, first.stdout + first.stderr
    cassettes = sorted(project.glob("cassettes/test_wire/*.json"))
    assert [c.name for c in cassettes] == ["test_flow.json"]
    assert "sk-abcdefghijklmnopqrstuv" not in cassettes[0].read_text()
    again = run_pytest(project, "--llm-cassette-mode=replay")
    # test_flow replays from the file; the chaos and name tests never touched a cassette file
    assert "3 passed" in again.stdout, again.stdout + again.stderr


def test_replay_mode_fails_when_no_cassette_exists(project: Path) -> None:
    result = run_pytest(project, "--llm-cassette-mode=replay")
    assert "1 failed" in result.stdout and "does not exist" in result.stdout


def test_dir_option_and_env_mode(project: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    target = tmp_path_factory.mktemp("elsewhere")
    result = run_pytest(project, f"--llm-cassette-dir={target}")
    assert "3 passed" in result.stdout, result.stdout + result.stderr
    assert (target / "test_wire" / "test_flow.json").exists()
    assert not (project / "cassettes").exists()
    replay = run_pytest(
        project, f"--llm-cassette-dir={target}", env={"PYTHON_WRAP_CASSETTE_MODE": "replay"}
    )
    assert "3 passed" in replay.stdout


def test_plugin_is_not_auto_registered() -> None:
    from importlib.metadata import entry_points

    names = {ep.value for ep in entry_points(group="pytest11")}
    assert "python_wrap.pytest_plugin" not in names
