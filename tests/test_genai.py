from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import sys
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from python_wrap import (
    AuditTape,
    Redactor,
    ReplayMismatch,
    _core,
    genai,
    retry,
    traced_llm,
    traced_tool,
)
from python_wrap.events import Event, listening
from python_wrap.observability import genai_span_attributes
from python_wrap.testing import fake_clock


def reply(text: str = "ok") -> dict[str, Any]:
    return {"text": text, "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_event_carries_context_tokens_cost_latency() -> None:
    clock_holder: list[Any] = []

    @traced_llm(model="demo-large", system="acme", prices={"demo": (1000.0, 2000.0)})
    def ask(prompt: str) -> dict[str, Any]:
        clock_holder[0].advance(0.5)
        return reply()

    events: list[Event] = []
    with fake_clock() as clock, listening(events.append), genai.context(run_id="r1", step=3):
        clock_holder.append(clock)
        ask("hi")
    (event,) = events
    f = event.fields
    assert event.name == "genai.llm_call"
    assert (f["run_id"], f["step"], f["model"], f["system"]) == ("r1", 3, "demo-large", "acme")
    assert (f["input_tokens"], f["output_tokens"]) == (10, 5)
    assert f["cost"] == pytest.approx((10 * 1000 + 5 * 2000) / 1e6)
    assert f["latency"] == pytest.approx(0.5)
    assert f["status"] == "ok"
    assert "input" not in f and "output" not in f  # metadata only by default


def test_usage_shapes_and_model_from_response() -> None:
    class Usage:
        prompt_tokens = 7
        completion_tokens = 2

    class Resp:
        usage = Usage()
        model = "from-response"

    assert genai.extract_usage(Resp()) == (7, 2)
    assert genai.extract_usage({"usage": {"input_tokens": 1, "output_tokens": 2}}) == (1, 2)
    assert genai.extract_usage("nope") == (None, None)
    assert genai.extract_usage("x", usage=lambda r: (4, 5)) == (4, 5)

    @traced_llm()
    def call() -> Resp:
        return Resp()

    events: list[Event] = []
    with listening(events.append):
        call()
    assert events[0].fields["model"] == "from-response"
    assert events[0].fields["cost"] is None


def test_tool_event_and_nested_context() -> None:
    seen: list[genai.RunContext] = []

    @traced_tool("search")
    def search(query: str) -> str:
        seen.append(genai.current_context())
        return "r"

    events: list[Event] = []
    with listening(events.append), genai.context(run_id="r9"):
        search("q")
    assert seen[0].tool == "search" and seen[0].run_id == "r9"
    assert genai.current_context().tool is None
    assert events[0].name == "genai.tool_call" and events[0].fields["tool"] == "search"


def test_agents_run_id_var_is_read_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    var: contextvars.ContextVar[str | None] = contextvars.ContextVar("v", default=None)
    monkeypatch.setitem(sys.modules, "python_wrap.agents", types.SimpleNamespace(run_id_var=var))
    assert genai.current_context().run_id is None
    token = var.set("agent-run")
    try:
        assert genai.current_context().run_id == "agent-run"
        with genai.context(run_id="explicit"):
            assert genai.current_context().run_id == "explicit"
    finally:
        var.reset(token)


def test_content_capture_is_opt_in_and_redacted() -> None:
    @traced_tool("send")
    def send(to: str, body: str, api_key: str) -> str:
        return "sent to " + to

    events: list[Event] = []
    with listening(events.append):
        send("bob@example.com", "card 4111 1111 1111 1111", "k-123")
        assert "input" not in events[0].fields
        events.clear()
        with genai.instrument(capture_content=True, otel=False):
            send("bob@example.com", "card 4111 1111 1111 1111 password=abc", "k-123")
    f = events[0].fields
    assert f["input"] == {
        "to": "[redacted:email]",
        "body": "card [redacted:card] password=[redacted:secret]",
        "api_key": "***",
    }
    assert f["output"] == "sent to [redacted:email]"


def test_instrument_restores_previous_defaults() -> None:
    with genai.instrument(capture_content=True, otel=False):
        assert genai._settings().capture_content
    assert not genai._settings().capture_content


def test_redactor_rules() -> None:
    r = Redactor(patterns=[r"ORD-\d+"], max_chars=20, max_items=2)
    assert r("order ORD-123 ok") == "order [redacted] ok"
    assert r("x" * 30) == "x" * 20 + "...[truncated 10 chars]"
    assert r([1, 2, 3, 4]) == [1, 2, "...[2 more items]"]
    plain = Redactor()
    assert plain("1234 5678 9012 3456") == "1234 5678 9012 3456"  # fails Luhn: not a card
    assert plain("Authorization: Bearer abcdefghij123") == "Authorization: Bearer [redacted:secret]"
    assert plain("key sk-abcdefghijklmnop1234") == "key [redacted:secret]"
    assert Redactor(builtin=False)("a@b.com") == "a@b.com"
    assert plain({"nested": {"password": "p", "ok": 1}}) == {"nested": {"password": "***", "ok": 1}}
    with pytest.raises(ValueError):
        Redactor(max_chars=0)


def test_redactor_handles_objects_and_cycles() -> None:
    @dataclasses.dataclass
    class Msg:
        role: str
        token: str

    cyc: list[Any] = []
    cyc.append(cyc)
    assert Redactor()(Msg("user", "t")) == {"role": "user", "token": "***"}
    assert Redactor()(cyc) == ["***"]
    assert Redactor()(b"abc") == "<3 bytes>"


def test_error_event_and_attempt_linkage() -> None:
    calls = 0

    @traced_tool("flaky")
    @retry(max_attempts=3, delay=0, on=ConnectionError)
    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("down")
        return "fine"

    @traced_tool("dead")
    @retry(max_attempts=2, delay=0, on=ConnectionError)
    def dead() -> str:
        raise ConnectionError("down")

    tape = AuditTape()
    events: list[Event] = []
    with fake_clock(), listening(events.append), genai.instrument(tape=tape, otel=False):
        assert flaky() == "fine"
        with pytest.raises(ConnectionError):
            dead()
    ok, bad = tape.records()
    assert [a["number"] for a in ok.attempts] == [1, 2] and ok.status == "ok"
    assert bad.status == "error" and bad.error is not None
    assert bad.error["type"] == "ConnectionError"
    assert [a["number"] for a in bad.attempts] == [1, 2]
    assert "duration" in bad.attempts[0]
    genai_events = [e for e in events if e.name.startswith("genai.")]
    assert genai_events[1].fields["status"] == "error" and genai_events[1].fields["attempts"] == 2
    assert all(item is not genai._collect for item in _core.listeners)


def test_async_functions() -> None:
    @traced_llm(model="m")
    async def ask(q: str) -> dict[str, Any]:
        return reply()

    events: list[Event] = []

    async def go() -> None:
        with genai.context(run_id="a"):
            await ask("x")

    with listening(events.append):
        asyncio.run(go())
    assert events[0].fields["run_id"] == "a" and events[0].fields["input_tokens"] == 10


@pytest.mark.parametrize("name", ["tape.jsonl", "tape.sqlite", None])
def test_tape_backends_roundtrip(tmp_path: Path, name: str | None) -> None:
    path = tmp_path / name if name else None

    @traced_tool("lookup")
    def lookup(key: str, password: str = "") -> dict[str, str]:
        return {"value": key.upper(), "email": "x@y.io"}

    with fake_clock(), AuditTape(path) as tape, genai.instrument(tape=tape, otel=False):
        with genai.context(run_id="r", step=1):
            lookup("abc", password="pw")
            lookup("def")
        records = tape.records()
    assert [r.seq for r in records] == [1, 2]
    assert records[0].input == {"key": "abc", "password": "***"}
    assert records[0].output == {"value": "ABC", "email": "[redacted:email]"}
    assert records[1].run_id == "r" and records[1].kind == "tool" and records[1].step == 1
    assert records[1].timestamp > 0
    if path is not None:
        with AuditTape(path) as again:
            assert again.records() == records  # persisted and ordered
            again.record(timestamp=1.0, kind="tool", name="n", function="f")
            assert [r.seq for r in again] == [1, 2, 3]
            again.clear()
            assert len(again) == 0


def test_tape_metadata_only_and_max_records() -> None:
    tape = AuditTape(capture_content=False, max_records=2)

    @traced_llm(model="m", tape=tape)
    def ask(q: str) -> dict[str, Any]:
        return reply("secret answer")

    for _ in range(3):
        ask("q")
    records = tape.records()
    assert len(records) == 2 and [r.seq for r in records] == [2, 3]
    assert records[0].input is None and records[0].output is None and not records[0].content
    assert records[0].input_tokens == 10
    with pytest.raises(ValueError):
        AuditTape(max_records=0)


def test_tape_redactor_applies_on_top() -> None:
    tape = AuditTape(redactor=Redactor(patterns=["PROJ-X"]))

    @traced_tool("t", tape=tape)
    def t(a: str) -> str:
        return "PROJ-X done"

    t("PROJ-X start")
    rec = tape.records()[0]
    assert rec.input == {"a": "[redacted] start"} and rec.output == "[redacted] done"


def test_tape_thread_safety_orders_sequence(tmp_path: Path) -> None:
    tape = AuditTape(tmp_path / "t.db")

    def add(i: int) -> None:
        tape.record(timestamp=0.0, kind="tool", name=str(i), function="f")

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(add, range(40)))
    assert sorted(r.seq for r in tape) == list(range(1, 41))
    tape.close()
    tape.close()


def _recorded_tape() -> AuditTape:
    tape = AuditTape()

    @traced_llm(model="m", tape=tape)
    def ask(prompt: str) -> dict[str, Any]:
        return reply(f"answer to {prompt}")

    @traced_tool("search", tape=tape)
    def search(query: str, limit: int = 3) -> list[str]:
        return [query] * limit

    @traced_tool("boom", tape=tape)
    def boom() -> None:
        raise KeyError("missing")

    ask("one")
    search("cats", limit=2)
    with pytest.raises(KeyError):
        boom()
    return tape


def test_replay_serves_in_order_and_checks_exhaustion() -> None:
    provider = genai.replay(_recorded_tape())
    assert provider.llm("one")["text"] == "answer to one"
    assert provider.tool("search")("cats", limit=2) == ["cats", "cats"]
    with pytest.raises(genai.ReplayedError) as info:
        provider.tool("boom")()
    assert info.value.error_type == "KeyError"
    provider.assert_exhausted()
    assert [r.seq for r in provider.calls] == [1, 2, 3]
    with pytest.raises(ReplayMismatch, match="no more records"):
        provider.llm("again")


def test_replay_mismatches() -> None:
    provider = genai.replay(_recorded_tape())
    with pytest.raises(ReplayMismatch, match="recorded llm"):
        provider.tool("search")()
    with pytest.raises(ReplayMismatch, match="never made"):
        provider.assert_exhausted()
    p2 = genai.replay(_recorded_tape())
    p2.llm("one")
    with pytest.raises(ReplayMismatch, match="'search'"):
        p2.tool("boom")()


def test_replay_check_inputs_and_unordered() -> None:
    p = genai.replay(_recorded_tape(), check_inputs=True)
    p.llm("one")
    with pytest.raises(ReplayMismatch, match="argument 'query'"):
        p.tool("search")("dogs", limit=2)
    assert p.tool("search")("cats", limit=2) == ["cats", "cats"]
    u = genai.replay(_recorded_tape(), ordered=False)
    assert u.tool("search")("x", limit=1) == ["cats", "cats"]
    assert u.llm("z")["text"] == "answer to one"
    with pytest.raises(ReplayMismatch):
        u.tool("search")()


def test_replay_context_manager_async_rebuild_and_paths(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    tape = AuditTape(path)

    @traced_llm(model="m", tape=tape)
    def ask(prompt: str) -> dict[str, Any]:
        return reply("hello")

    ask("p")
    upper: Callable[[genai.TapeRecord], Any] = lambda r: r.output["text"].upper()  # noqa: E731
    provider = genai.replay(path, rebuild=upper)
    assert asyncio.run(provider.allm("p")) == "HELLO"
    with pytest.raises(ReplayMismatch, match="never made"), genai.replay(tape):
        pass
    with genai.replay(tape.records()) as ok:
        ok.llm()
    meta = AuditTape(capture_content=False)

    @traced_tool("t", tape=meta)
    def t() -> str:
        return "x"

    t()
    with pytest.raises(ReplayMismatch, match="metadata only"):
        genai.replay(meta).tool("t")()


def test_atool_replay() -> None:
    tape = AuditTape()

    @traced_tool("t", tape=tape)
    def t() -> int:
        return 5

    t()
    assert asyncio.run(genai.replay(tape).atool("t")()) == 5


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}
        self.ended: int | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def end(self, end_time: int | None = None) -> None:
        self.ended = end_time


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, _Span]] = []

    def start_span(self, name: str, start_time: int | None = None) -> _Span:
        span = _Span()
        self.spans.append((name, span))
        return span


def test_otel_attributes_and_spans() -> None:
    tracer = _Tracer()

    @traced_llm(model="m", system="acme")
    def ask(q: str) -> dict[str, Any]:
        return reply()

    @traced_tool("search")
    def search() -> int:
        return 1

    with genai.instrument(tracer=tracer, capture_content=True), genai.context(run_id="r"):
        ask("hi")
        search()
    (n1, s1), (n2, s2) = tracer.spans
    assert n1 == "chat m" and n2 == "execute_tool search"
    assert s1.attrs["gen_ai.system"] == "acme"
    assert s1.attrs["gen_ai.request.model"] == "m"
    assert s1.attrs["gen_ai.usage.input_tokens"] == 10
    assert s1.attrs["gen_ai.operation.name"] == "chat" and s1.attrs["python_wrap.run_id"] == "r"
    assert "gen_ai.input.messages" in s1.attrs
    assert s2.attrs["gen_ai.tool.name"] == "search" and s1.ended is not None
    attrs = genai_span_attributes(Event("genai.tool_call", "f", {"tool": "t", "cost": None}))
    assert attrs == {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "t"}


def test_otel_absent_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with genai.instrument() as handle:
        assert handle.otel is None


def test_recording_failure_never_breaks_the_call() -> None:
    class Broken(AuditTape):
        def record(self, **fields: Any) -> Any:
            raise RuntimeError("disk full")

    @traced_tool("t", tape=Broken())
    def t() -> int:
        return 1

    assert t() == 1


def test_price_prefix_matching() -> None:
    prices = {"demo": (1.0, 1.0), "demo-large": (2.0, 2.0)}
    assert genai._cost(prices, "demo-large-2", 1_000_000, 0) == 2.0
    assert genai._cost(prices, "other", 1, 1) is None
    assert genai._cost(prices, "demo", None, None) is None
