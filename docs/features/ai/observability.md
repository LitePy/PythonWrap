# AI observability and audit

Trace LLM and tool calls, keep secrets out of logs, and record an audit tape you can replay in tests. Nothing here imports a provider SDK; responses are read by duck typing.

```python
from python_wrap import AuditTape, genai, replay, traced_llm, traced_tool
from python_wrap.events import listening

tape = AuditTape()
events = []

@traced_llm(model="demo-1", system="demo", prices={"demo": (3.0, 15.0)}, tape=tape)
def ask(prompt: str) -> dict:
    return {"text": "hi", "usage": {"input_tokens": 100, "output_tokens": 20}}

@traced_tool("lookup", tape=tape)
def lookup(email: str, api_key: str) -> dict:
    return {"contact": email}

with listening(events.append), genai.context(run_id="run-1", step=1):
    ask("hello bob@example.com")
    lookup("bob@example.com", api_key="secret-value")

assert events[0].name == "genai.llm_call"
assert events[0].fields["input_tokens"] == 100
assert events[0].fields["cost"] > 0
assert "input" not in events[0].fields          # content capture is off by default
assert tape.records()[1].input["api_key"] == "***"

with replay(tape) as provider:
    assert provider.llm("anything")["text"] == "hi"
    assert provider.tool("lookup")("x", api_key="y")["contact"] == "[redacted:email]"
```

## Events and run context

`traced_llm(model=...)` and `traced_tool(name)` publish `genai.llm_call` / `genai.tool_call` events with run id, step, tool, model, input and output tokens, cost, latency, status and the failed attempts of an inner `retry`. Tokens are read from `usage.input_tokens/output_tokens`, `usage.prompt_tokens/completion_tokens` (objects or dicts), or a `usage=` callable. Cost needs a price table `{model: (usd_per_million_in, usd_per_million_out)}`; a model matches exactly or by longest prefix.

Run identity lives in the `genai.run_context` context variable. Set it with `genai.context(run_id=..., step=...)`. If `python_wrap.agents` is loaded its `run_id_var` is used when no run id is set.

`genai.instrument(...)` sets process-wide defaults (`capture_content`, `redactor`, `prices`, `tape`) and, when OpenTelemetry is installed, emits spans with GenAI semantic-convention attributes (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.name`). Without OpenTelemetry it does nothing extra. Close the returned handle (or use `with`) to restore the previous defaults.

## Redaction

`Redactor` masks secrets (keys, bearer tokens, JWTs, `password=...`), emails, Luhn-valid card numbers, your own `patterns`, and values under sensitive key names (the same rules as `python_wrap.redaction`). It truncates long strings (`max_chars`) and long lists (`max_items`). Content is **not** captured in events or spans unless you pass `capture_content=True`; the tape is an explicit sink and keeps redacted content unless you create it with `capture_content=False`.

## Audit tape

`AuditTape()` keeps records in memory; `AuditTape("tape.jsonl")` appends JSON lines; a `.db`, `.sqlite` or `.sqlite3` path uses SQLite. Each `TapeRecord` has `seq`, `timestamp`, `kind`, `name`, run context, status, redacted `input`/`output`, error type, tokens, cost, latency and `attempts` (the retry history linked via `attempt_history` or `retry.*` events).

## Replay

`replay(tape)` returns a `ReplayProvider`: `provider.llm(...)`, `provider.tool("name")(...)` and async `allm` / `atool`. Calls are served in recorded order and raise `ReplayMismatch` on an unexpected call, a different kind or tool name, leftover records (`assert_exhausted()`, or leaving the `with` block), and, with `check_inputs=True`, different arguments. Failed recordings raise `ReplayedError`. Outputs are the recorded JSON-shaped values; pass `rebuild=` to build richer objects. Use `ordered=False` to match by kind and name regardless of order.
