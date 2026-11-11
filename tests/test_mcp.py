"""The MCP server, driven over real stdio pipes and in process."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from python_wrap.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSIONS,
    Server,
)

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = textwrap.dedent(
    """
    import os
    from python_wrap import RetryBudget, policy, retry, timeout

    print("noise printed at import time")

    safe = policy(
        name="safe",
        retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
        timeout=timeout(2),
    )
    charge = policy(name="charge", retry=retry(max_attempts=3, on=ConnectionError))

    @retry(max_attempts=3, on=ConnectionError)
    @timeout(1)
    def fetch() -> str:
        return "ok"

    NOT_CALLABLE = 3
    """
)


class Pipe:
    """A subprocess MCP client speaking newline-delimited JSON-RPC."""

    def __init__(self, cwd: Path, *args: str) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "python_wrap", "mcp", *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.ids = 0

    def raw(self, line: bytes) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line + b"\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def request(self, method: str, params: Any = None) -> Any:
        self.ids += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.ids, "method": method}
        if params is not None:
            message["params"] = params
        reply = self.raw(json.dumps(message).encode())
        assert reply["id"] == self.ids
        return reply

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, tool: str, **arguments: Any) -> Any:
        return self.request("tools/call", {"name": tool, "arguments": arguments})["result"]

    def close(self) -> tuple[int, bytes, bytes]:
        out, err = self.proc.communicate(timeout=20)
        return self.proc.returncode, out, err


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "sampleapp").mkdir()
    (tmp_path / "sampleapp" / "__init__.py").write_text("")
    (tmp_path / "sampleapp" / "stacks.py").write_text(SAMPLE)
    (tmp_path / "othermod.py").write_text("from sampleapp.stacks import safe\n")
    return tmp_path


@pytest.fixture
def pipe(project: Path) -> Iterator[Pipe]:
    client = Pipe(project)
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    client.notify("notifications/initialized")
    yield client
    status, _, _ = client.close()
    assert status == 0


def test_handshake_and_tools_list(pipe: Pipe) -> None:
    assert pipe.request("ping")["result"] == {}
    tools = pipe.request("tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "explain_policy",
        "doctor",
        "migrate_preview",
        "search_api",
        "get_symbol",
        "recipe",
        "guide",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["description"]


def test_doctor_on_policies_and_functions(pipe: Pipe) -> None:
    safe = pipe.call("doctor", import_path="sampleapp.stacks:safe")
    assert safe["isError"] is False
    data = safe["structuredContent"]
    assert data["ok"] and data["findings"] == []
    assert [layer["role"] for layer in data["layers"]] == ["retry", "timeout"]
    assert json.loads(safe["content"][0]["text"]) == data

    charge = pipe.call("doctor", import_path="sampleapp.stacks:charge", idempotent=False)
    codes = [item["code"] for item in charge["structuredContent"]["findings"]]
    assert "PW002" in codes and not charge["structuredContent"]["ok"]

    fetch = pipe.call("doctor", import_path="sampleapp.stacks:fetch", multiprocess=True)
    assert fetch["structuredContent"]["layers"][0]["role"] == "retry"


def test_explain_policy_includes_canonical_order(pipe: Pipe) -> None:
    result = pipe.call("explain_policy", import_path="sampleapp.stacks:safe")
    data = result["structuredContent"]
    assert data["canonical_order"][:2] == ["observe", "validate"]
    assert "safe:" in data["report"]


def test_import_noise_never_reaches_stdout(project: Path) -> None:
    client = Pipe(project)
    result = client.call("doctor", import_path="sampleapp.stacks:safe")
    assert result["isError"] is False
    status, out, err = client.close()
    assert status == 0 and out == b""
    assert b"noise printed at import time" in err


@pytest.mark.parametrize(
    ("import_path", "message"),
    [
        ("sampleapp.stacks", "expected package.module:attribute"),
        ("../etc:passwd", "expected package.module:attribute"),
        ("sampleapp.stacks:safe; rm -rf /", "expected package.module:attribute"),
        ("not_installed_anywhere:x", "not importable"),
        ("sampleapp.stacks:missing", "not found"),
        ("sampleapp.stacks:safe.__class__", "dunder"),
        ("sampleapp.stacks:NOT_CALLABLE", "not a Policy"),
    ],
)
def test_bad_import_paths_are_tool_errors(pipe: Pipe, import_path: str, message: str) -> None:
    result = pipe.call("doctor", import_path=import_path)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


def test_allow_restricts_imports(project: Path) -> None:
    client = Pipe(project, "--allow", "sampleapp")
    assert client.call("doctor", import_path="sampleapp.stacks:safe")["isError"] is False
    denied = client.call("doctor", import_path="othermod:safe")
    assert denied["isError"] is True and "outside --allow" in denied["content"][0]["text"]
    status, _, _ = client.close()
    assert status == 0


def test_migrate_preview_never_writes(pipe: Pipe, project: Path) -> None:
    before = sorted(p.name for p in project.rglob("*"))
    source = (
        "import tenacity\n\n@tenacity.retry(stop=tenacity.stop_after_attempt(3))\n"
        "def f():\n    pass\n"
    )
    data = pipe.call("migrate_preview", source=source, filename="jobs.py")["structuredContent"]
    assert data["changed"] is True
    assert data["diff"].startswith("--- a/jobs.py")
    assert data["changes"][0]["line"] == 3
    unparsable = pipe.call("migrate_preview", source="def (:\n")["structuredContent"]
    assert not unparsable["changed"] and unparsable["unsupported"]
    after = sorted(p.name for p in project.rglob("*") if "__pycache__" not in p.parts)
    assert [n for n in after if n != "__pycache__"] == [n for n in before if n != "__pycache__"]


def test_lookup_tools(pipe: Pipe) -> None:
    found = pipe.call("search_api", query="circuit breaker", limit=3)["structuredContent"]
    assert "circuit_breaker" in [item["name"] for item in found["results"]]
    symbol = pipe.call("get_symbol", name="retry")["structuredContent"]
    assert symbol["qualname"] == "python_wrap.retry" and "doc" not in symbol
    missing = pipe.call("get_symbol", name="retri")
    assert missing["isError"] is True and "closest" in missing["content"][0]["text"]
    recipe = pipe.call("recipe", need="stop duplicate emails")["structuredContent"]
    assert recipe["recipes"][0]["apis"][0]["name"] == "idempotent"
    nothing = pipe.call("recipe", need="zzzz qqqq")["structuredContent"]
    assert nothing["recipes"] == []
    guide = pipe.call("guide")
    assert "What never to retry" in guide["content"][0]["text"]


def test_protocol_errors(pipe: Pipe) -> None:
    assert pipe.raw(b"{not json")["error"]["code"] == PARSE_ERROR
    assert pipe.raw(b"[]")["error"]["code"] == INVALID_REQUEST
    assert pipe.raw(b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}')["error"]["code"] == (
        INVALID_REQUEST
    )
    assert pipe.raw(b'{"jsonrpc": "2.0", "id": 1}')["error"]["code"] == INVALID_REQUEST
    assert pipe.request("resources/list")["error"]["code"] == METHOD_NOT_FOUND
    assert pipe.request("tools/call", {"name": "nope"})["error"]["code"] == INVALID_PARAMS
    bad_type = pipe.request("tools/call", {"name": "doctor", "arguments": {"import_path": 3}})
    assert bad_type["error"]["code"] == INVALID_PARAMS
    missing = pipe.request("tools/call", {"name": "doctor", "arguments": {}})
    assert missing["error"]["code"] == INVALID_PARAMS
    extra = pipe.request("tools/call", {"name": "guide", "arguments": {"path": "/etc/passwd"}})
    assert extra["error"]["code"] == INVALID_PARAMS
    bool_limit = pipe.request(
        "tools/call", {"name": "search_api", "arguments": {"query": "x", "limit": True}}
    )
    assert bool_limit["error"]["code"] == INVALID_PARAMS
    # Still serving after all of that.
    assert pipe.request("ping")["result"] == {}


def test_unknown_protocol_version_gets_the_latest(project: Path) -> None:
    client = Pipe(project)
    init = client.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert init["result"]["serverInfo"]["name"] == "python-wrap"
    client.close()


def test_in_process_serve_handles_notifications_blank_lines_and_size_limit() -> None:
    lines = [
        b"",
        b'{"jsonrpc": "2.0", "method": "notifications/initialized"}',
        b'{"jsonrpc": "2.0", "method": "notifications/unknown"}',
        b'{"jsonrpc": "2.0", "id": "a", "result": {}}',
        b"x" * ((1 << 20) + 10),
        b'{"jsonrpc": "2.0", "id": "b", "method": "ping"}',
    ]
    stdin = io.BytesIO(b"\n".join(lines) + b"\n")
    stdout = io.BytesIO()
    server = Server()
    server.serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert server.initialized
    assert [reply.get("id") for reply in replies] == [None, "b"]
    assert replies[0]["error"]["code"] == INVALID_REQUEST
    assert replies[1]["result"] == {}


def test_invalid_ids_and_params() -> None:
    server = Server()
    assert server.handle({"jsonrpc": "2.0", "id": None, "method": "ping"}) is not None
    reply = server.handle({"jsonrpc": "2.0", "id": True, "method": "ping"})
    assert reply is not None and reply["error"]["code"] == INVALID_REQUEST
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]})
    assert reply is not None and reply["error"]["code"] == INVALID_PARAMS
    assert server.handle("ping") == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": INVALID_REQUEST, "message": "Invalid Request"},
    }


def test_oversized_migrate_source_is_rejected() -> None:
    reply = Server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "migrate_preview", "arguments": {"source": "x" * (2 << 20)}},
        }
    )
    assert reply is not None and reply["result"]["isError"] is True


def test_migrate_preview_reports_llm_retry_loops(pipe: Pipe) -> None:
    source = (
        "import time\n\n\ndef ask(client):\n    for n in range(3):\n        try:\n"
        "            return client.messages.create(model='m')\n"
        "        except Exception:\n            time.sleep(1)\n"
    )
    data = pipe.call("migrate_preview", source=source)["structuredContent"]
    assert data["changed"] is True and "@llm_retry(max_attempts=3, delay=1)" in data["diff"]
    assert data["changes"][0]["line"] == 5
