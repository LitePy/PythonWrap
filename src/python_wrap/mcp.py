"""A dependency-free, read-only Model Context Protocol server: ``python -m python_wrap mcp``.

It speaks MCP over stdio (JSON-RPC 2.0, one JSON message per line) and exposes tools a
coding agent can call while it writes code that uses python-wrap:

========================  ==========================================================
``explain_policy``        Layers and findings of a policy or decorated function.
``doctor``                The same lint with ``idempotent`` / ``multiprocess`` flags.
``migrate_preview``       Dry-run of the tenacity/backoff codemod on source text.
``search_api``            Search public names and docstrings.
``get_symbol``            Signature, parameters, raises and example of one name.
``recipe``                Map a plain-language need to the right API.
``guide``                 The "how to use python-wrap correctly" guide.
========================  ==========================================================

Security: the server never writes files, opens sockets or runs subprocesses, and never
calls the code it inspects. ``import_path`` arguments (``package.module:attribute``) must
name a module that is already importable in the server's environment; importing it runs
that module's top-level code, exactly like ``python -m python_wrap doctor``. Start the
server only in a project you trust, and pass ``--allow PREFIX`` to restrict imports to
your own packages. Anything printed while importing goes to stderr, never into the
protocol stream.

Example:
    ```python
    from python_wrap.mcp import Server

    server = Server()
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    names = [tool["name"] for tool in reply["result"]["tools"]]
    assert "doctor" in names and "get_symbol" in names
    ```
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import importlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any

__all__ = ["PROTOCOL_VERSIONS", "Server", "main"]

#: Protocol revisions this server understands, newest first.
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_MAX_LINE = 1 << 20  # bytes per message
_MAX_SOURCE = 1 << 20  # characters for migrate_preview
_IMPORT_PATH = re.compile(
    r"^(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):(?P<attr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)$"
)
_INSTRUCTIONS = (
    "Read-only helpers for python-wrap (resilience decorators). Call `guide` once before "
    "writing code, `recipe` or `search_api` to choose an API, `get_symbol` for exact "
    "parameters, and `doctor` on the policy or function you wrote."
)


class ToolError(Exception):
    """A tool failed in a way the caller should see (reported with ``isError: true``)."""


class _InvalidParams(Exception):
    pass


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    properties: Mapping[str, Mapping[str, Any]]
    required: tuple[str, ...]
    handler: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": dict(self.properties),
                "required": list(self.required),
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }


_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    from python_wrap.introspect import _render_value

    return _render_value(value)


class Server:
    """The MCP request handler; `serve` runs it over a pair of byte streams.

    Args:
        allow: Module prefixes ``import_path`` may name (``"myapp"`` allows ``myapp`` and
            ``myapp.clients``). Empty means any module importable in this environment.
    """

    def __init__(self, allow: Sequence[str] = ()) -> None:
        self.allow = tuple(prefix.strip(".") for prefix in allow if prefix.strip("."))
        self.initialized = False
        target = {
            "type": "string",
            "description": "package.module:attribute of a Policy or decorated function",
        }
        self._tools = {
            tool.name: tool
            for tool in (
                _Tool(
                    "explain_policy",
                    "Describe a python-wrap Policy or decorated function: its layers "
                    "outermost first with settings, the canonical layer order, and findings.",
                    {"import_path": target},
                    ("import_path",),
                    self._explain_policy,
                ),
                _Tool(
                    "doctor",
                    "Lint a python-wrap Policy or decorated function (codes PW001-PW017). "
                    "Set idempotent=false for payments, emails and other side effects; "
                    "multiprocess=true when it runs in several worker processes.",
                    {
                        "import_path": target,
                        "idempotent": {"type": "boolean", "default": True},
                        "multiprocess": {"type": "boolean", "default": False},
                    },
                    ("import_path",),
                    self._doctor,
                ),
                _Tool(
                    "migrate_preview",
                    "Dry-run the python-wrap codemod on Python source text: tenacity/backoff "
                    "decorators and hand-rolled LLM retry loops. Returns a unified diff, "
                    "notes and suggestions; never writes files.",
                    {
                        "source": {"type": "string", "description": "Python source code"},
                        "filename": {"type": "string", "default": "<source>"},
                    },
                    ("source",),
                    self._migrate_preview,
                ),
                _Tool(
                    "search_api",
                    "Search python-wrap public names and docstrings.",
                    {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10, "minimum": 1},
                    },
                    ("query",),
                    self._search_api,
                ),
                _Tool(
                    "get_symbol",
                    "Signature, summary, parameters, raises and a minimal example for one "
                    "public name, such as 'retry', 'presets.http_client' or "
                    "'CircuitBreaker.reset'.",
                    {"name": {"type": "string"}},
                    ("name",),
                    self._get_symbol,
                ),
                _Tool(
                    "recipe",
                    "Map a plain-language need ('retry http calls', 'stop duplicate "
                    "emails') to the python-wrap API, with a runnable snippet and pitfalls.",
                    {
                        "need": {"type": "string"},
                        "limit": {"type": "integer", "default": 3, "minimum": 1},
                    },
                    ("need",),
                    self._recipe,
                ),
                _Tool(
                    "guide",
                    "How to use python-wrap correctly: layer order, idempotency rules, what "
                    "never to retry, common mistakes, doctor usage.",
                    {},
                    (),
                    self._guide,
                ),
            )
        }

    # ------------------------------------------------------------------ tools

    def _resolve(self, import_path: str) -> Any:
        match = _IMPORT_PATH.match(import_path.strip())
        if not match:
            raise ToolError(f"expected package.module:attribute, got {import_path!r}")
        module_name, attr = match.group("module"), match.group("attr")
        if any(part.startswith("__") for part in attr.split(".")):
            raise ToolError("dunder attributes are not allowed")
        if self.allow and not any(
            module_name == prefix or module_name.startswith(prefix + ".") for prefix in self.allow
        ):
            raise ToolError(f"module {module_name!r} is outside --allow {list(self.allow)}")
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError) as exc:
            raise ToolError(f"module {module_name!r} is not importable: {exc}") from None
        if spec is None:
            raise ToolError(f"module {module_name!r} is not importable in this environment")
        try:
            obj: Any = importlib.import_module(module_name)
        except Exception as exc:
            raise ToolError(f"importing {module_name!r} failed: {type(exc).__name__}") from None
        for part in attr.split("."):
            if not hasattr(obj, part):
                raise ToolError(f"{module_name}:{attr} not found ({part!r} missing)")
            obj = getattr(obj, part)
        return obj

    def _report(self, import_path: str, idempotent: bool, multiprocess: bool) -> dict[str, Any]:
        from python_wrap.doctor import explain

        target = self._resolve(import_path)
        if not callable(target):  # a Policy is callable too
            raise ToolError(f"{import_path} is not a Policy or a decorated function")
        report = explain(target, idempotent=idempotent, multiprocess=multiprocess)
        return {
            "target": report.target,
            "ok": report.ok,
            "layers": [
                {"role": layer.role, "settings": _json_safe(dict(layer.settings))}
                for layer in report.layers
            ],
            "findings": [
                {"code": item.code, "severity": item.severity, "message": item.message}
                for item in report.findings
            ],
            "report": str(report),
        }

    def _explain_policy(self, import_path: str) -> dict[str, Any]:
        from python_wrap.policy import ROLES

        data = self._report(import_path, True, False)
        data["canonical_order"] = list(ROLES)
        return data

    def _doctor(
        self, import_path: str, idempotent: bool = True, multiprocess: bool = False
    ) -> dict[str, Any]:
        return self._report(import_path, idempotent, multiprocess)

    def _migrate_preview(self, source: str, filename: str = "<source>") -> dict[str, Any]:
        from python_wrap.migrate import migrate_source

        if len(source) > _MAX_SOURCE:
            raise ToolError(f"source is larger than {_MAX_SOURCE} characters")
        result = migrate_source(source, filename)
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                result.text.splitlines(keepends=True),
                f"a/{filename}",
                f"b/{filename}",
            )
        )
        return {
            "changed": result.text != source,
            "diff": diff,
            "changes": [
                {
                    "line": item.line,
                    "before": item.before,
                    "after": item.after,
                    "notes": list(item.notes),
                }
                for item in result.changes
            ],
            "unsupported": [
                {"line": item.line, "message": item.message} for item in result.unsupported
            ],
        }

    def _search_api(self, query: str, limit: int = 10) -> dict[str, Any]:
        from python_wrap.introspect import search

        found = search(query, limit=min(limit, 50))
        return {"results": [entry.to_dict(full=False) for entry in found]}

    def _get_symbol(self, name: str) -> dict[str, Any]:
        from python_wrap.introspect import get_symbol, search

        entry = get_symbol(name)
        if entry is None:
            hints = [item.name for item in search(name, limit=5)]
            raise ToolError(f"unknown symbol {name!r}; closest: {hints}")
        data = entry.to_dict()
        data.pop("doc")
        return data

    def _recipe(self, need: str, limit: int = 3) -> dict[str, Any]:
        from python_wrap.introspect import find_recipes, search

        found = find_recipes(need, limit=min(limit, 20))
        related = [] if found else [e.to_dict(full=False) for e in search(need, limit=5)]
        return {"recipes": [item.to_dict() for item in found], "related_apis": related}

    def _guide(self) -> str:
        from python_wrap.introspect import guide_text

        return guide_text()

    # ------------------------------------------------------------------ protocol

    def _call_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _InvalidParams(f"Unknown tool: {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise _InvalidParams("arguments must be an object")
        for key, value in arguments.items():
            spec = tool.properties.get(key)
            if spec is None:
                raise _InvalidParams(f"unexpected argument {key!r} for {tool.name}")
            expected = _TYPES[spec["type"]]
            if not isinstance(value, expected) or (
                spec["type"] == "integer" and isinstance(value, bool)
            ):
                raise _InvalidParams(f"argument {key!r} must be a {spec['type']}")
            if spec["type"] == "integer" and value < spec.get("minimum", value):
                raise _InvalidParams(f"argument {key!r} must be >= {spec['minimum']}")
        missing = [key for key in tool.required if key not in arguments]
        if missing:
            raise _InvalidParams(f"missing required argument(s) {missing} for {tool.name}")
        try:
            with contextlib.redirect_stdout(sys.stderr):
                output = tool.handler(**arguments)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # report, never crash the server
            message = f"{tool.name} failed: {type(exc).__name__}: {exc}"
            return {"content": [{"type": "text", "text": message}], "isError": True}
        if isinstance(output, str):
            return {"content": [{"type": "text", "text": output}], "isError": False}
        return {
            "content": [{"type": "text", "text": json.dumps(output, indent=2)}],
            "structuredContent": output,
            "isError": False,
        }

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        from python_wrap import __version__

        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "python-wrap", "title": "python-wrap", "version": __version__},
            "instructions": _INSTRUCTIONS,
        }

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message; return the response, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Invalid Request")
        request_id = message.get("id")
        method = message.get("method")
        is_notification = "id" not in message
        if not isinstance(method, str):
            if is_notification or "result" in message or "error" in message:
                return None  # a response to a request we never send: ignore it
            return _error(request_id, INVALID_REQUEST, "Invalid Request: missing method")
        if not is_notification and (
            request_id is None
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
        ):
            return _error(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer")
        params = message.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if is_notification:
                return None
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        if is_notification:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            if method == "initialize":
                result: dict[str, Any] = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool.schema() for tool in self._tools.values()]}
            elif method == "tools/call":
                result = self._call_tool(params)
            else:
                return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except _InvalidParams as exc:
            return _error(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:
            return _error(request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, stdin: IO[bytes], stdout: IO[bytes]) -> None:
        """Read newline-delimited JSON-RPC from ``stdin`` until EOF, answering on ``stdout``."""
        while True:
            line = stdin.readline(_MAX_LINE + 1)
            if not line:
                return
            if len(line) > _MAX_LINE and not line.endswith(b"\n"):
                while True:  # discard the rest of the oversized message
                    rest = stdin.readline(_MAX_LINE)
                    if not rest or rest.endswith(b"\n"):
                        break
                reply: dict[str, Any] | None = _error(None, INVALID_REQUEST, "message too large")
            elif not line.strip():
                continue
            else:
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    reply = _error(None, PARSE_ERROR, "Parse error")
                else:
                    if isinstance(message, list):
                        reply = _error(None, INVALID_REQUEST, "batches are not supported")
                    else:
                        reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
                stdout.flush()


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m python_wrap mcp [--allow PREFIX]...``: serve MCP over stdio until EOF."""
    parser = argparse.ArgumentParser(
        prog="python -m python_wrap mcp",
        description="Read-only Model Context Protocol server for python-wrap (stdio).",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PREFIX",
        help="only import modules under this package (repeatable); default: any importable",
    )
    args = parser.parse_args(argv)
    if "" not in sys.path:
        sys.path.insert(0, "")
    out = sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):  # stray prints must not corrupt the stream
        Server(args.allow).serve(sys.stdin.buffer, out)
    return 0
