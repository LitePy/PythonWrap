"""Observability and compliance for LLM and tool calls: enriched events, redaction, audit tape.

Three pieces, none of which imports a provider SDK or any other optional package:

* `traced_llm` / `traced_tool` wrap a function and publish a ``genai.llm_call`` /
  ``genai.tool_call`` event carrying run id, step, tool, model, token counts, cost and
  latency. `instrument` turns on process-wide defaults (content capture, redaction,
  price table, audit tape, OpenTelemetry GenAI spans).
* `Redactor` scrubs prompts, tool arguments and outputs (secrets, emails, card numbers,
  your own patterns, sensitive key names) and truncates long text. Content capture is
  **off** unless you ask for it: by default events carry metadata only.
* `AuditTape` records every call (redacted, ordered, timestamped) to memory, a JSONL file
  or SQLite; `replay` turns a tape into fake callables that serve the recorded results in
  tests and raise `ReplayMismatch` when the code under test diverges.

Run identity travels in the `run_context` context variable. Agent frameworks and your own
code set it with `context`; if `python_wrap.agents` is loaded, its ``run_id_var`` is read
as a fallback for the run id.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import dataclasses
import functools
import inspect
import json
import re
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from python_wrap import _core, _fork
from python_wrap.errors import PythonWrapError
from python_wrap.events import Event, subscribe
from python_wrap.redaction import MASK, is_sensitive

__all__ = [
    "AuditTape",
    "Instrumentation",
    "Redactor",
    "ReplayMismatch",
    "ReplayProvider",
    "ReplayedError",
    "RunContext",
    "TapeRecord",
    "context",
    "current_context",
    "extract_usage",
    "instrument",
    "replay",
    "run_context",
    "traced_llm",
    "traced_tool",
]

P = ParamSpec("P")
R = TypeVar("R")

Prices = Mapping[str, tuple[float, float]]


# --------------------------------------------------------------------------- run context


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Who a call belongs to: the agent run, the step in it, the tool and the model.

    Attributes:
        run_id: Identifier of the run (any string), or None.
        step: Step number within the run, or None.
        tool: Name of the tool being executed, or None.
        model: Name of the model being called, or None.
    """

    run_id: str | None = None
    step: int | None = None
    tool: str | None = None
    model: str | None = None


#: Current `RunContext`. Set it directly (``run_context.set(...)``) or use `context`.
run_context: contextvars.ContextVar[RunContext] = contextvars.ContextVar(
    "python_wrap_genai_run_context",
    default=RunContext(),  # noqa: B039 - frozen dataclass
)

_UNSET: Any = object()


@contextmanager
def context(
    *,
    run_id: str | None = _UNSET,
    step: int | None = _UNSET,
    tool: str | None = _UNSET,
    model: str | None = _UNSET,
) -> Iterator[RunContext]:
    """Set fields of the current `RunContext` for a ``with`` block.

    Fields you do not pass keep their current value.

    Example:
        >>> with context(run_id="r1", step=2) as ctx:
        ...     assert current_context().run_id == "r1"
    """
    changes: dict[str, Any] = {
        key: value
        for key, value in (("run_id", run_id), ("step", step), ("tool", tool), ("model", model))
        if value is not _UNSET
    }
    new = dataclasses.replace(run_context.get(), **changes)
    token = run_context.set(new)
    try:
        yield new
    finally:
        run_context.reset(token)


def current_context() -> RunContext:
    """The active `RunContext`; the run id falls back to ``python_wrap.agents.run_id_var``.

    That module is only consulted if something else already imported it.
    """
    ctx = run_context.get()
    if ctx.run_id is None:
        module = sys.modules.get("python_wrap.agents")
        var = getattr(module, "run_id_var", None)
        if var is not None:
            with contextlib.suppress(Exception):
                found = var.get(None)
                if found is not None:
                    return dataclasses.replace(ctx, run_id=str(found))
    return ctx


# --------------------------------------------------------------------------- redaction

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
_BUILTIN_SECRETS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
    ),
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}"),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token|authorization)(\s*[=:]\s*)(?!\*\*\*|bearer\b|basic\b|\[redacted)[^\s,;'\"}]+"
)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


class Redactor:
    """Scrub values before they reach logs, traces or an audit tape.

    Strings are scanned for secrets (private keys, JWTs, provider key shapes, bearer
    tokens, ``password=...`` assignments), email addresses and card numbers (Luhn
    checked), plus your own ``patterns``. Mappings mask the value of any sensitive key
    (see `python_wrap.redaction.is_sensitive`). Anything that is not JSON-shaped is
    converted first (dataclasses, ``model_dump()``/``to_dict()`` objects, else ``repr``),
    and strings longer than ``max_chars`` are cut after scrubbing.

    Args:
        fragments: Sensitive key-name fragments; defaults to `DEFAULT_REDACT`.
        patterns: Extra regular expressions (strings or compiled); matches are masked.
        max_chars: Longest string kept (>= 1).
        max_items: Longest list/tuple/set kept (>= 1); the rest is dropped with a marker.
        max_depth: Container levels walked; deeper values are masked.
        builtin: Set False to disable the built-in text patterns (key names still apply).
        mask: Replacement for key-based masks.

    Example:
        >>> Redactor()({"note": "mail bob@example.com", "api_key": "k"})
        {'note': 'mail [redacted:email]', 'api_key': '***'}
    """

    def __init__(
        self,
        *,
        fragments: Iterable[str] | None = None,
        patterns: Iterable[str | re.Pattern[str]] = (),
        max_chars: int = 2000,
        max_items: int = 100,
        max_depth: int = 12,
        builtin: bool = True,
        mask: str = MASK,
    ) -> None:
        if max_chars < 1 or max_items < 1 or max_depth < 0:
            raise ValueError("max_chars and max_items must be >= 1 and max_depth >= 0")
        self.fragments = None if fragments is None else tuple(fragments)
        self.patterns = tuple(re.compile(p) if isinstance(p, str) else p for p in patterns)
        self.max_chars, self.max_items, self.max_depth = max_chars, max_items, max_depth
        self.builtin, self.mask = builtin, mask

    def scrub_text(self, text: str) -> str:
        """Mask secrets, emails, card numbers and custom patterns in ``text``; then truncate."""
        if self.builtin:
            for pattern in _BUILTIN_SECRETS:
                text = pattern.sub("[redacted:secret]", text)
            text = _BEARER.sub(lambda m: f"{m.group(1)} [redacted:secret]", text)
            text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted:secret]", text)
            text = _EMAIL.sub("[redacted:email]", text)
            text = _CARD.sub(
                lambda m: "[redacted:card]" if _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0),
                text,
            )
        for pattern in self.patterns:
            text = pattern.sub("[redacted]", text)
        if len(text) > self.max_chars:
            text = f"{text[: self.max_chars]}...[truncated {len(text) - self.max_chars} chars]"
        return text

    def __call__(self, value: Any) -> Any:
        """Return a scrubbed, JSON-shaped copy of ``value``."""
        return self._walk(value, 0, frozenset())

    def _walk(self, value: Any, depth: int, ancestors: frozenset[int]) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            converted = _convert(value)
            if converted is _UNSET:
                return self.scrub_text(repr(value))
            value = converted
            if isinstance(value, str):
                return self.scrub_text(value)
        if depth > self.max_depth or id(value) in ancestors:
            return self.mask
        ancestors = ancestors | {id(value)}
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                masked = is_sensitive(name, self.fragments)
                out[name] = self.mask if masked else self._walk(item, depth + 1, ancestors)
            return out
        items = list(value)
        kept = [self._walk(item, depth + 1, ancestors) for item in items[: self.max_items]]
        if len(items) > self.max_items:
            kept.append(f"...[{len(items) - self.max_items} more items]")
        return kept


def _convert(value: Any) -> Any:
    """Turn a known object shape into plain data; `_UNSET` when it has none."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name, None) for f in dataclasses.fields(value)}
    for method in ("model_dump", "to_dict", "dict"):
        func = getattr(value, method, None)
        if callable(func):
            with contextlib.suppress(Exception):
                result = func()
                if isinstance(result, Mapping):
                    return dict(result)
    return _UNSET


# --------------------------------------------------------------------------- usage and cost


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_usage(
    response: Any, usage: Callable[[Any], tuple[int | None, int | None]] | None = None
) -> tuple[int | None, int | None]:
    """Read ``(input_tokens, output_tokens)`` from a provider response without any SDK.

    Understands ``usage.input_tokens/output_tokens`` (Anthropic style) and
    ``usage.prompt_tokens/completion_tokens`` (OpenAI style) on objects or dicts, and a
    ``usage`` mapping at the top level. A ``usage`` callable you supply wins.

    Example:
        >>> extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 3}})
        (10, 3)
    """
    if usage is not None:
        return usage(response)
    block = _field(response, "usage")
    if block is None:
        return None, None
    first = _count(_field(block, "input_tokens"))
    second = _count(_field(block, "output_tokens"))
    if first is None:
        first = _count(_field(block, "prompt_tokens"))
    if second is None:
        second = _count(_field(block, "completion_tokens"))
    return first, second


def _cost(
    prices: Prices | None, model: str | None, tin: int | None, tout: int | None
) -> float | None:
    if not prices or model is None or (tin is None and tout is None):
        return None
    key = (
        model
        if model in prices
        else max((k for k in prices if model.startswith(k)), key=len, default=None)
    )
    if key is None:
        return None
    per_in, per_out = prices[key]
    return ((tin or 0) * per_in + (tout or 0) * per_out) / 1_000_000


# --------------------------------------------------------------------------- audit tape


@dataclasses.dataclass(frozen=True)
class TapeRecord:
    """One recorded call.

    Attributes:
        seq: 1-based position in the tape.
        timestamp: Wall-clock start time (seconds since the epoch).
        kind: ``"llm"`` or ``"tool"``.
        name: Tool name, or model name for LLM calls (falls back to the function name).
        function: Qualified name of the traced function.
        run_id / step / model / tool: The `RunContext` at call time.
        status: ``"ok"`` or ``"error"``.
        input: Redacted arguments as a dict (None when content capture is off).
        output: Redacted result (None when content capture is off or the call failed).
        error: ``{"type": ..., "message": ...}`` for a failed call (message only with content).
        input_tokens / output_tokens / cost / latency: Metrics.
        attempts: Failed attempts of an inner `retry`: dicts with ``number``, ``exception``,
            ``next_delay`` and, when available, ``duration``.
        content: Whether input/output were captured.
    """

    seq: int
    timestamp: float
    kind: str
    name: str
    function: str
    run_id: str | None = None
    step: int | None = None
    model: str | None = None
    tool: str | None = None
    status: str = "ok"
    input: Any = None
    output: Any = None
    error: dict[str, str] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    latency: float = 0.0
    attempts: tuple[dict[str, Any], ...] = ()
    content: bool = False

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable dict."""
        data = dataclasses.asdict(self)
        data["attempts"] = list(self.attempts)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TapeRecord:
        """Rebuild a record from `to_dict` output; unknown keys are ignored."""
        names = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in names}
        values["attempts"] = tuple(values.get("attempts") or ())
        return cls(**values)


class AuditTape:
    """An ordered, redacted log of LLM and tool calls, in memory, JSONL or SQLite.

    Args:
        path: ``None`` keeps records in memory. Otherwise a file: ``.db``, ``.sqlite`` or
            ``.sqlite3`` selects SQLite, anything else JSONL (one JSON object per line).
            An existing file is appended to and its records are readable.
        capture_content: Keep redacted inputs and outputs. The tape is an explicit sink and
            replay needs results, so this defaults to True; pass False for metadata only.
        redactor: Applied to inputs, outputs and error messages recorded here, on top of
            whatever the caller already scrubbed.
        max_records: For a memory tape, keep only the newest N records.

    Thread-safe. Use `close` (or ``with``) to release a SQLite connection.

    Example:
        >>> tape = AuditTape()
        >>> len(tape)
        0
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        capture_content: bool = True,
        redactor: Redactor | None = None,
        max_records: int | None = None,
    ) -> None:
        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be positive")
        self.capture_content = capture_content
        self.redactor = redactor
        self.max_records = max_records
        self.path = None if path is None else Path(path)
        self._lock = _fork.ForkSafeLock()
        self._memory: list[TapeRecord] = []
        self._seq = 0
        self._db: sqlite3.Connection | None = None
        self.backend: Literal["memory", "jsonl", "sqlite"] = "memory"
        if self.path is not None:
            if self.path.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
                self.backend = "sqlite"
                self._db = sqlite3.connect(self.path, check_same_thread=False)
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS tape "
                    "(seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)"
                )
                self._db.commit()
            else:
                self.backend = "jsonl"
            self._seq = len(self._read())

    def _read(self) -> list[TapeRecord]:
        if self.backend == "sqlite" and self._db is not None:
            rows = self._db.execute("SELECT data FROM tape ORDER BY seq").fetchall()
            return [TapeRecord.from_dict(json.loads(row[0])) for row in rows]
        if self.backend == "jsonl" and self.path is not None and self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                return [TapeRecord.from_dict(json.loads(line)) for line in handle if line.strip()]
        return list(self._memory)

    def record(self, **fields: Any) -> TapeRecord:
        """Append a call. Takes `TapeRecord` fields (``seq`` is assigned); returns the record.

        Content is dropped when ``capture_content`` is False and scrubbed with the tape's
        ``redactor`` if there is one.
        """
        fields.pop("seq", None)
        if not self.capture_content:
            fields.update(input=None, output=None, content=False)
            if fields.get("error"):
                fields["error"] = {"type": fields["error"].get("type", "")}
        elif self.redactor is not None:
            for key in ("input", "output"):
                if fields.get(key) is not None:
                    fields[key] = self.redactor(fields[key])
            if fields.get("error"):
                fields["error"] = {k: self.redactor(v) for k, v in fields["error"].items()}
        with self._lock:
            self._seq += 1
            entry = TapeRecord(seq=self._seq, **fields)
            if self.backend == "sqlite" and self._db is not None:
                self._db.execute(
                    "INSERT INTO tape (data) VALUES (?)", (json.dumps(entry.to_dict()),)
                )
                self._db.commit()
            elif self.backend == "jsonl" and self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry.to_dict()) + "\n")
            else:
                self._memory.append(entry)
                if self.max_records is not None and len(self._memory) > self.max_records:
                    del self._memory[: len(self._memory) - self.max_records]
        return entry

    def records(self) -> list[TapeRecord]:
        """All records, oldest first."""
        with self._lock:
            return self._read()

    def __len__(self) -> int:
        with self._lock:
            return len(self._read())

    def __iter__(self) -> Iterator[TapeRecord]:
        return iter(self.records())

    def clear(self) -> None:
        """Delete every record (a file backend is emptied)."""
        with self._lock:
            self._memory.clear()
            self._seq = 0
            if self.backend == "sqlite" and self._db is not None:
                self._db.execute("DELETE FROM tape")
                self._db.commit()
            elif self.backend == "jsonl" and self.path is not None:
                self.path.write_text("", encoding="utf-8")

    def close(self) -> None:
        """Release the SQLite connection, if any. Safe to call twice."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> AuditTape:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- instrumentation


@dataclasses.dataclass(frozen=True)
class _Settings:
    capture_content: bool = False
    redactor: Redactor = dataclasses.field(default_factory=Redactor)
    prices: Prices | None = None
    tape: AuditTape | None = None


_settings_lock = _fork.ForkSafeLock()
_active: _Settings | None = None


class Instrumentation:
    """Handle returned by `instrument`; `close()` restores the previous defaults."""

    def __init__(self, settings: _Settings, previous: _Settings | None) -> None:
        self.settings = settings
        self._previous = previous
        self.closed = False
        #: The OpenTelemetry adapter subscription, or None when OTel is absent or disabled.
        self.otel: Any = None

    def close(self) -> None:
        global _active
        if self.closed:
            return
        self.closed = True
        if self.otel is not None:
            self.otel.close()
        with _settings_lock:
            if _active is self.settings:
                _active = self._previous

    def __enter__(self) -> Instrumentation:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def instrument(
    *,
    capture_content: bool = False,
    redactor: Redactor | None = None,
    prices: Prices | None = None,
    tape: AuditTape | None = None,
    otel: bool = True,
    tracer: Any = None,
    tracer_provider: Any = None,
) -> Instrumentation:
    """Set process-wide defaults for `traced_llm` and `traced_tool`, and emit OTel spans.

    Args:
        capture_content: Put redacted inputs and outputs in events and spans. Off by
            default: events then carry metadata only.
        redactor: Scrubbing rules for captured content; a default `Redactor` if omitted.
        prices: ``{model: (usd_per_million_input_tokens, usd_per_million_output_tokens)}``.
            A model matches exactly or by longest key prefix.
        tape: An `AuditTape` that records every traced call.
        otel: Create OpenTelemetry GenAI spans when ``opentelemetry`` is installed; silently
            nothing otherwise.
        tracer: A tracer to use instead of the global provider's.
        tracer_provider: A tracer provider to use instead of the global one.

    Returns:
        An `Instrumentation` handle; use it as a context manager or call `close()`.
    """
    global _active
    settings = _Settings(capture_content, redactor or Redactor(), prices, tape)
    with _settings_lock:
        previous = _active
        _active = settings
    handle = Instrumentation(settings, previous)
    if otel:
        from python_wrap import observability

        handle.otel = observability.enable_genai(tracer=tracer, tracer_provider=tracer_provider)
    return handle


def _settings() -> _Settings:
    return _active or _Settings()


# Retry events seen while a traced call runs are attached to its record. The listener is
# subscribed only while at least one traced call is in flight.
_collectors: contextvars.ContextVar[tuple[list[dict[str, Any]], ...]] = contextvars.ContextVar(
    "python_wrap_genai_collectors", default=()
)
_collect_lock = _fork.ForkSafeLock()
_collect_users = 0
_collect_off: Callable[[], None] | None = None


def _collect(event: Event) -> None:
    if event.name != "retry.attempt_failed":
        return
    exc = event.fields.get("exception")
    entry = {
        "number": event.fields.get("attempt"),
        "exception": type(exc).__qualname__ if exc is not None else "rejected_result",
        "next_delay": event.fields.get("next_delay"),
    }
    for sink in _collectors.get():
        sink.append(entry)


def _collect_enter() -> None:
    global _collect_users, _collect_off
    with _collect_lock:
        _collect_users += 1
        if _collect_users == 1:
            _collect_off = subscribe(_collect)


def _collect_exit() -> None:
    global _collect_users, _collect_off
    with _collect_lock:
        _collect_users -= 1
        if _collect_users == 0 and _collect_off is not None:
            _collect_off()
            _collect_off = None


def _bind(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": dict(kwargs)}
    named = dict(bound.arguments)
    if named and next(iter(named)) in ("self", "cls"):
        del named[next(iter(named))]
    return named


def _history(exc: BaseException, collected: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    from python_wrap.retry import attempt_history

    records = attempt_history(exc)
    if records:
        return tuple(
            {
                "number": r.number,
                "exception": r.exception,
                "next_delay": r.next_delay,
                "duration": r.duration,
            }
            for r in records
        )
    return tuple(collected)


def _finish(
    kind: Literal["llm", "tool"],
    func: Callable[..., Any],
    name: str,
    model: str | None,
    system: str | None,
    options: dict[str, Any],
    call: tuple[tuple[Any, ...], dict[str, Any]],
    ctx: RunContext,
    started: float,
    latency: float,
    collected: list[dict[str, Any]],
    result: Any,
    exc: BaseException | None,
) -> None:
    """Build the event and tape record. Never raises."""
    try:
        base = _settings()
        capture = (
            base.capture_content
            if options["capture_content"] is None
            else options["capture_content"]
        )
        redactor: Redactor = options["redactor"] or base.redactor
        prices = options["prices"] or base.prices
        tape: AuditTape | None = options["tape"] if options["tape"] is not None else base.tape
        tin = tout = None
        if exc is None and kind == "llm":
            tin, tout = extract_usage(result, options["usage"])
            if model is None:
                found = _field(result, "model")
                model = found if isinstance(found, str) else None
        cost = _cost(prices, model, tin, tout) if kind == "llm" else None
        attempts = _history(exc, collected) if exc is not None else tuple(collected)
        error_type = None if exc is None else type(exc).__qualname__
        need_content = capture or (tape is not None and tape.capture_content)
        content_in = content_out = error_message = None
        if need_content:
            content_in = redactor(_bind(func, *call))
            if exc is None:
                content_out = redactor(result)
            else:
                error_message = redactor.scrub_text(str(exc))
        fields: dict[str, Any] = {
            "kind": kind,
            "run_id": ctx.run_id,
            "step": ctx.step,
            "tool": name if kind == "tool" else ctx.tool,
            "model": model,
            "system": system,
            "input_tokens": tin,
            "output_tokens": tout,
            "cost": cost,
            "latency": latency,
            "started": started,
            "status": "ok" if exc is None else "error",
            "error_type": error_type,
            "attempts": len(attempts) or None,
        }
        if capture:
            fields.update(input=content_in, output=content_out, error=error_message)
        if kind == "tool":
            _core.emit("genai.tool_call", _core.qualname(func), **fields)
        else:
            _core.emit("genai.llm_call", _core.qualname(func), **fields)
        if tape is not None:
            tape.record(
                timestamp=started,
                kind=kind,
                name=name,
                function=_core.qualname(func),
                run_id=ctx.run_id,
                step=ctx.step,
                model=model,
                tool=fields["tool"],
                status=fields["status"],
                input=content_in,
                output=content_out,
                error=None
                if exc is None
                else {"type": error_type or "", "message": error_message or ""},
                input_tokens=tin,
                output_tokens=tout,
                cost=cost,
                latency=latency,
                attempts=attempts,
                content=need_content,
            )
    except Exception:
        _core.get_logger().exception("python_wrap.genai failed to record a call")


def _wrap(
    kind: Literal["llm", "tool"],
    name: str | None,
    model: str | None,
    system: str | None,
    options: dict[str, Any],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        label = name or getattr(func, "__name__", None) or "call"
        shown = label if kind == "tool" else (model or label)

        def begin() -> tuple[RunContext, list[dict[str, Any]], Any, Any, float, float]:
            ctx = current_context()
            inner = dataclasses.replace(
                ctx, **({"tool": label} if kind == "tool" else {"model": model or ctx.model})
            )
            collected: list[dict[str, Any]] = []
            _collect_enter()
            ctoken = _collectors.set((*_collectors.get(), collected))
            rtoken = run_context.set(inner)
            return ctx, collected, ctoken, rtoken, _core.wall_time(), _core.perf_counter()

        def end(ctoken: Any, rtoken: Any) -> None:
            run_context.reset(rtoken)
            _collectors.reset(ctoken)
            _collect_exit()

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                ctx, collected, ctoken, rtoken, started, t0 = begin()
                try:
                    result = await func(*args, **kwargs)  # type: ignore[misc]
                except BaseException as exc:
                    end(ctoken, rtoken)
                    _finish(kind, func, shown, model, system, options, (args, kwargs), ctx,
                            started, _core.perf_counter() - t0, collected, None, exc)  # fmt: skip
                    raise
                end(ctoken, rtoken)
                _finish(kind, func, shown, model, system, options, (args, kwargs), ctx,
                        started, _core.perf_counter() - t0, collected, result, None)  # fmt: skip
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx, collected, ctoken, rtoken, started, t0 = begin()
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                end(ctoken, rtoken)
                _finish(kind, func, shown, model, system, options, (args, kwargs), ctx,
                        started, _core.perf_counter() - t0, collected, None, exc)  # fmt: skip
                raise
            end(ctoken, rtoken)
            _finish(kind, func, shown, model, system, options, (args, kwargs), ctx,
                    started, _core.perf_counter() - t0, collected, result, None)  # fmt: skip
            return result

        return wrapper

    return decorator


def traced_llm(
    model: str | None = None,
    *,
    system: str | None = None,
    usage: Callable[[Any], tuple[int | None, int | None]] | None = None,
    prices: Prices | None = None,
    capture_content: bool | None = None,
    redactor: Redactor | None = None,
    tape: AuditTape | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a function that calls an LLM; publish a ``genai.llm_call`` event per call.

    The event (and the `AuditTape` record) carries the run context, model, input and
    output token counts (read from the response, see `extract_usage`), cost when a price
    table knows the model, latency, status and the attempts of an inner `retry`. Sync and
    async functions are supported. While the call runs, ``run_context.model`` is set.

    Args:
        model: Model name; if omitted it is read from the response's ``model`` field.
        system: Provider name for ``gen_ai.system`` (for example ``"openai"``).
        usage: ``response -> (input_tokens, output_tokens)`` for unusual response shapes.
        prices: Per-call price table; falls back to `instrument`'s.
        capture_content: Override `instrument`'s content-capture default.
        redactor: Override the redactor.
        tape: Record to this tape in addition to `instrument`'s.

    Example:
        >>> @traced_llm(model="demo-1")
        ... def ask(prompt: str) -> dict:
        ...     return {"text": "hi", "usage": {"input_tokens": 3, "output_tokens": 1}}
        >>> ask("hello")["text"]
        'hi'
    """
    options = {
        "usage": usage, "prices": prices, "capture_content": capture_content,
        "redactor": redactor, "tape": tape,
    }  # fmt: skip
    return _wrap("llm", None, model, system, options)


def traced_tool(
    name: str | None = None,
    *,
    capture_content: bool | None = None,
    redactor: Redactor | None = None,
    tape: AuditTape | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a tool function; publish a ``genai.tool_call`` event per call.

    Same enrichment as `traced_llm` without tokens or cost. ``run_context.tool`` is set to
    the tool name while it runs, so nested LLM calls and events know which tool they are in.

    Args:
        name: Tool name; defaults to the function's name.
        capture_content: Override `instrument`'s content-capture default.
        redactor: Override the redactor.
        tape: Record to this tape in addition to `instrument`'s.
    """
    options = {
        "usage": None, "prices": None, "capture_content": capture_content,
        "redactor": redactor, "tape": tape,
    }  # fmt: skip
    return _wrap("tool", name, None, None, options)


# --------------------------------------------------------------------------- replay


class ReplayMismatch(PythonWrapError, AssertionError):
    """The code under test made a call that does not match the next recorded one."""


class ReplayedError(RuntimeError):
    """Raised by a replayed call that failed when recorded.

    Attributes:
        error_type: The original exception's qualified type name.
    """

    def __init__(self, error_type: str, message: str = "") -> None:
        super().__init__(f"{error_type}: {message}" if message else error_type)
        self.error_type = error_type


class ReplayProvider:
    """Fake callables that serve a tape's recorded results; build one with `replay`.

    Attributes:
        llm: Callable serving the next recorded LLM call (any arguments).
        allm: Async version of ``llm``.
        calls: Records served so far, in order.
    """

    def __init__(
        self,
        records: Sequence[TapeRecord],
        *,
        ordered: bool = True,
        check_inputs: bool = False,
        redactor: Redactor | None = None,
        rebuild: Callable[[TapeRecord], Any] | None = None,
    ) -> None:
        self._records = list(records)
        self._used = [False] * len(self._records)
        self._cursor = 0
        self.ordered, self.check_inputs = ordered, check_inputs
        self._redactor = redactor or Redactor()
        self._rebuild = rebuild
        self._lock = _fork.ForkSafeLock()
        self.calls: list[TapeRecord] = []

    @property
    def remaining(self) -> list[TapeRecord]:
        """Recorded calls that have not been served yet."""
        with self._lock:
            return [r for r, used in zip(self._records, self._used, strict=True) if not used]

    def assert_exhausted(self) -> None:
        """Raise `ReplayMismatch` if recorded calls were never made."""
        left = self.remaining
        if left:
            raise ReplayMismatch(
                f"{len(left)} recorded call(s) were never made; "
                f"next: {left[0].kind} {left[0].name!r}"
            )

    def _serve(
        self, kind: str, name: str | None, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        want = f"{kind} {name!r}" if name else kind
        with self._lock:
            if self.ordered:
                if self._cursor >= len(self._records):
                    raise ReplayMismatch(f"unexpected {want} call: the tape has no more records")
                index = self._cursor
                record = self._records[index]
                if record.kind != kind or (name is not None and record.name != name):
                    raise ReplayMismatch(
                        f"call #{index + 1} is {want}, but the tape recorded "
                        f"{record.kind} {record.name!r}"
                    )
            else:
                index = next(
                    (
                        i
                        for i, r in enumerate(self._records)
                        if not self._used[i] and r.kind == kind and (name is None or r.name == name)
                    ),
                    -1,
                )
                if index < 0:
                    raise ReplayMismatch(f"unexpected {want} call: no unused recorded call matches")
                record = self._records[index]
            if self.check_inputs:
                self._compare(record, args, kwargs)
            if record.status == "ok" and record.output is None and not record.content:
                raise ReplayMismatch(
                    f"record #{record.seq} has no captured output (tape recorded metadata only)"
                )
            self._used[index] = True
            self._cursor = index + 1
            self.calls.append(record)
        if record.status == "error":
            error = record.error or {}
            raise ReplayedError(error.get("type", "Error"), error.get("message", ""))
        if self._rebuild is not None:
            return self._rebuild(record)
        return copy.deepcopy(record.output)

    def _compare(self, record: TapeRecord, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        recorded = record.input
        if not isinstance(recorded, dict):
            return
        names = list(recorded)
        given = {names[i]: v for i, v in enumerate(args) if i < len(names)}
        given.update(kwargs)
        scrubbed = self._redactor(given)
        for key, value in scrubbed.items():
            if key in recorded and recorded[key] != value:
                raise ReplayMismatch(
                    f"call #{record.seq} ({record.name!r}): argument {key!r} was "
                    f"{value!r}, the tape recorded {recorded[key]!r}"
                )

    def llm(self, *args: Any, **kwargs: Any) -> Any:
        """Serve the next recorded LLM call."""
        return self._serve("llm", None, args, kwargs)

    async def allm(self, *args: Any, **kwargs: Any) -> Any:
        """Async `llm`."""
        return self._serve("llm", None, args, kwargs)

    def tool(self, name: str) -> Callable[..., Any]:
        """A callable serving the recorded calls of tool ``name``."""

        def call(*args: Any, **kwargs: Any) -> Any:
            return self._serve("tool", name, args, kwargs)

        call.__name__ = name
        return call

    def atool(self, name: str) -> Callable[..., Awaitable[Any]]:
        """Async `tool`."""

        async def call(*args: Any, **kwargs: Any) -> Any:
            return self._serve("tool", name, args, kwargs)

        call.__name__ = name
        return call

    def __enter__(self) -> ReplayProvider:
        return self

    def __exit__(self, exc_type: object, *rest: object) -> None:
        if exc_type is None:
            self.assert_exhausted()


def replay(
    tape: AuditTape | Iterable[TapeRecord] | str | Path,
    *,
    ordered: bool = True,
    check_inputs: bool = False,
    redactor: Redactor | None = None,
    rebuild: Callable[[TapeRecord], Any] | None = None,
) -> ReplayProvider:
    """Turn a recorded tape into fake callables for deterministic tests.

    Results are the recorded (redacted, JSON-shaped) outputs. Used as a ``with`` block the
    provider also checks that every recorded call was made.

    Args:
        tape: An `AuditTape`, a path to a JSONL/SQLite tape, or `TapeRecord` objects.
        ordered: Calls must come in the recorded order (default). With False each call is
            matched to the first unused record of its kind and name.
        check_inputs: Also compare the arguments (after redaction) with the recorded input.
            Positional arguments are matched by position, keywords by name.
        redactor: Used for ``check_inputs``; should match the one used when recording.
        rebuild: ``record -> object`` to return something richer than the recorded JSON.

    Raises:
        ReplayMismatch: (from the callables) on an unexpected call, a different kind or tool
            name, different inputs, or (from `assert_exhausted`) leftover records.
        ReplayedError: (from the callables) when the recorded call had failed.
    """
    if isinstance(tape, (str, Path)):
        with AuditTape(tape) as opened:
            records: Sequence[TapeRecord] = opened.records()
    elif isinstance(tape, AuditTape):
        records = tape.records()
    else:
        records = list(tape)
    return ReplayProvider(
        records, ordered=ordered, check_inputs=check_inputs, redactor=redactor, rebuild=rebuild
    )
