"""Drive the python-wrap MCP server like an AI coding agent would.

Spawns ``python -m python_wrap mcp`` over stdio, performs the MCP handshake, lists the
tools, asks the doctor about two policies defined in this file and asks for a recipe.
Standard library only; the server never writes files or opens sockets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from python_wrap import RetryBudget, policy, retry, timeout

# Sample policies the server imports as ``mcp_client_demo:catalog`` and ``:charge``.
catalog = policy(
    name="catalog",
    retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
    timeout=timeout(2),
)
charge = policy(name="charge", retry=retry(max_attempts=3, on=ConnectionError), timeout=timeout(5))


class Client:
    """A minimal newline-delimited JSON-RPC client for an MCP stdio server."""

    def __init__(self, *command: str, cwd: Path) -> None:
        self.proc = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.next_id = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id, reply
        assert "error" not in reply, reply["error"]
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
        assert self.proc.stdin
        self.proc.stdin.close()  # EOF ends the server
        return self.proc.wait(timeout=10)


def main() -> None:
    client = Client(sys.executable, "-m", "python_wrap", "mcp", cwd=Path(__file__).parent)
    try:
        init = client.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "demo", "version": "1.0"},
            },
        )
        client.notify("notifications/initialized")
        print(f"connected to {init['serverInfo']['name']} {init['serverInfo']['version']}")

        tools = [tool["name"] for tool in client.send("tools/list")["tools"]]
        print("tools:", ", ".join(tools))
        assert {"doctor", "get_symbol", "recipe", "migrate_preview"} <= set(tools)

        good = client.call("doctor", import_path="mcp_client_demo:catalog")
        print(good["report"])
        assert good["ok"] and [layer["role"] for layer in good["layers"]] == ["retry", "timeout"]

        bad = client.call("doctor", import_path="mcp_client_demo:charge", idempotent=False)
        print(bad["report"])
        assert not bad["ok"] and "PW002" in [item["code"] for item in bad["findings"]]

        recipe = client.call("recipe", need="stop duplicate emails")["recipes"][0]
        print("recipe:", recipe["need"], "->", [api["name"] for api in recipe["apis"]])
        assert recipe["apis"][0]["name"] == "idempotent"
    finally:
        status = client.close()
    assert status == 0
    print("MCP demo passed")


if __name__ == "__main__":
    main()
