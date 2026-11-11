"""Story: the command line tools and the MCP server work for a person or a coding agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]

SAMPLE = """\
from python_wrap import RetryBudget, policy, retry, timeout

catalog = policy(
    name="catalog",
    retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
    timeout=timeout(2),
)
charge = policy(name="charge", retry=retry(max_attempts=3, on=ConnectionError), timeout=timeout(5))
"""

LEGACY = """\
from tenacity import retry, stop_after_attempt, wait_fixed


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def fetch():
    return 1
"""


@pytest.fixture
def env() -> dict[str, str]:
    src = str(ROOT / "src")
    return {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "sample_stacks.py").write_text(SAMPLE)
    (tmp_path / "legacy.py").write_text(LEGACY)
    return tmp_path


def run_cli(
    env: dict[str, str], cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "python_wrap", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def test_doctor_passes_a_good_stack_and_fails_a_bad_one(env: dict[str, str], workdir: Path) -> None:
    good = run_cli(env, workdir, "doctor", "sample_stacks:catalog")
    assert "catalog" in good.stdout and "PW0" not in good.stdout
    bad = run_cli(env, workdir, "doctor", "--non-idempotent", "sample_stacks:charge", check=False)
    assert bad.returncode == 1
    assert "PW002" in bad.stdout


def test_api_index_and_symbol_lookup(env: dict[str, str], workdir: Path) -> None:
    listing = run_cli(env, workdir, "api", "--json")
    names = {entry["name"] for entry in json.loads(listing.stdout)}
    assert {"retry", "timeout", "CircuitBreaker"} <= names
    one = run_cli(env, workdir, "api", "retry")
    assert "retry" in one.stdout
    missing = run_cli(env, workdir, "api", "no_such_symbol", check=False)
    assert missing.returncode == 1


def test_recipe_maps_a_need_to_an_api(env: dict[str, str], workdir: Path) -> None:
    result = run_cli(env, workdir, "recipe", "stop", "duplicate", "emails", "--json")
    recipes = json.loads(result.stdout)
    assert recipes[0]["apis"][0]["name"] == "idempotent"


def test_guide_prints_the_agent_guide(env: dict[str, str], workdir: Path) -> None:
    result = run_cli(env, workdir, "guide")
    assert len(result.stdout) > 500 and "python-wrap" in result.stdout.lower()


def test_migrate_previews_then_rewrites(env: dict[str, str], workdir: Path) -> None:
    preview = run_cli(env, workdir, "migrate", "--diff", "legacy.py")
    assert "max_attempts=3" in preview.stdout
    assert "tenacity" in (workdir / "legacy.py").read_text()  # nothing written yet
    run_cli(env, workdir, "migrate", "--write", "legacy.py")
    text = (workdir / "legacy.py").read_text()
    assert "tenacity" not in text and "from python_wrap import retry" in text


class McpClient:
    """Newline-delimited JSON-RPC over the server's stdio."""

    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "python_wrap", "mcp"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.next_id = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id and "error" not in reply, reply
        return reply["result"]

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, tool: str, **arguments: Any) -> Any:
        result = self.send("tools/call", {"name": tool, "arguments": arguments})
        assert result["isError"] is False, result["content"]
        return result.get("structuredContent", result["content"][0]["text"])

    def close(self) -> int:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.close()
        status = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        return status


def test_mcp_server_handshake_tools_and_doctor(env: dict[str, str], workdir: Path) -> None:
    client = McpClient(env, workdir)
    try:
        init = client.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1.0"},
            },
        )
        client.notify("notifications/initialized")
        assert init["serverInfo"]["name"]

        tools = {tool["name"] for tool in client.send("tools/list")["tools"]}
        assert {"doctor", "get_symbol", "recipe", "migrate_preview"} <= tools

        good = client.call("doctor", import_path="sample_stacks:catalog")
        assert good["ok"] and [layer["role"] for layer in good["layers"]] == ["retry", "timeout"]
        bad = client.call("doctor", import_path="sample_stacks:charge", idempotent=False)
        assert not bad["ok"] and "PW002" in [item["code"] for item in bad["findings"]]
    finally:
        status = client.close()
    assert status == 0
