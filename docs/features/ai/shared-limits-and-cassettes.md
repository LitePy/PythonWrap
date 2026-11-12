# Shared limits, validated output and cassettes

Three problems show up once an LLM-backed service runs in more than one process or gets a real test suite:

- N workers each think they own the whole tokens-per-minute quota, so together they cause a 429 storm.
- The model returns JSON that does not validate, and every retry resends (and pays for) the whole prompt.
- Tests either hit the real API or mock it so far from the wire that the SDK's own error handling is never exercised.

This page covers the pieces that fix them. All of it has zero dependencies: `httpx`, `redis` and `pydantic` are never imported at module import time.

## One token bucket for every worker

`token_rate_limit` keeps its bucket in memory. Pass `store=` a shared window and the bucket lives in storage every worker reaches.

```python
import os
import tempfile

from python_wrap.llm import token_rate_limit
from python_wrap.llm_shared import SQLiteTokenWindow

path = os.path.join(tempfile.mkdtemp(), "tokens.db")

# In every worker process: the same file, the same limit.
limit = token_rate_limit(60_000, estimate=2_000, block=False, store=SQLiteTokenWindow(path))

@limit
def complete(prompt: str) -> dict[str, object]:
    return {"usage": {"input_tokens": 500, "output_tokens": 100}}

complete("hello")  # reserves 2000, settles at the real 600, for the whole fleet
assert 59_000 < limit.available < 59_500
```

| Window | Shares across | Notes |
| --- | --- | --- |
| `SQLiteTokenWindow(path, name=...)` | processes on one host | WAL journal, one `BEGIN IMMEDIATE` transaction per operation; a corrupt file is moved to `<path>.corrupt` and rebuilt |
| `RedisTokenWindow(client, name=...)` | every host | one Lua script per operation, using the Redis server's clock; keys expire by themselves |
| `AsyncRedisTokenWindow(client, name=...)` | every host, async | the same script awaited on a `redis.asyncio` client |

Every worker must use the same `tokens_per_minute`; the window name picks the bucket, so one file or Redis can hold several. A sync window used from an `async def` runs its round trip in a worker thread.

`SQLiteBudgetBackend(path)` is a `BudgetBackend` on the same idea: `Budget(tokens=..., backend=SQLiteBudgetBackend(path))` shares its counters across local processes, with `period=` expiry.

### When storage fails

By default a window **fails open**: if the file is locked or Redis is unreachable it emits `rate_limit.store_error` and uses a private in-memory bucket for that call, so an outage degrades to per-process limiting instead of stopping every LLM call. Pass `fail_open=False` to raise instead.

### Backing off together

`observe_headers(headers)` feeds a provider's own numbers into the window. It understands `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens` (`"6m0s"`, `"20ms"`), the `anthropic-ratelimit-*` family (RFC 3339 reset times, separate input and output limits), `retry-after` (seconds or an HTTP date) and `retry-after-ms`. The remaining count only ever lowers the bucket. `Retry-After` (or the reset time when no tokens remain) blocks every worker until then.

```python
import os
import tempfile

from python_wrap.errors import RateLimitExceeded
from python_wrap.llm import token_rate_limit
from python_wrap.llm_shared import SQLiteTokenWindow, parse_rate_limit_headers

path = os.path.join(tempfile.mkdtemp(), "tokens.db")
worker_a = token_rate_limit(60_000, estimate=100, block=False, store=SQLiteTokenWindow(path))
worker_b = token_rate_limit(60_000, estimate=100, block=False, store=SQLiteTokenWindow(path))

worker_a.observe_headers({"retry-after": "30"})  # worker A saw a 429
try:
    worker_b(lambda: "sent")()  # worker B waits as well
except RateLimitExceeded as exc:
    assert 29 < exc.retry_after <= 30

hint = parse_rate_limit_headers({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "2s"})
assert hint.block_seconds == 2
```

A decorated function that raises an error carrying response headers (`exc.headers` or `exc.response.headers`, as SDK status errors do) feeds them back automatically, so a 429 needs no extra code. Use `await limit.aobserve_headers(...)` from async code.

## Validated structured output

`validated(parse)` wraps a call that should return structured text. `parse` is any callable that raises on bad output: `Model.model_validate_json` from pydantic (a class with that method is used directly), a JSON-schema checker, or `json.loads` plus your own checks.

```python
import json

from python_wrap.llm_validate import ValidationRepairExhausted, validated

replies = iter(['{"city": ', '{"city": "Oslo"}'])  # cut off, then fixed
sent = []

def parse(text: str) -> dict[str, str]:
    data = json.loads(text)
    if "city" not in data:
        raise ValueError("missing 'city'")
    return data

@validated(parse, max_repairs=2)
def ask(messages: list[dict[str, str]]) -> str:
    sent.append(messages)
    return next(replies)

assert ask(messages=[{"role": "user", "content": "Where?"}]) == {"city": "Oslo"}
assert sent[1][-1]["role"] == "user"  # the repair turn carries only the error text
assert sent[1][-2] == {"role": "assistant", "content": '{"city": '}

@validated(parse, max_repairs=1)
def hopeless(messages: list[dict[str, str]]) -> str:
    return "not json"

try:
    hopeless(messages=[])
except ValidationRepairExhausted as exc:
    assert len(exc.attempts) == 2 and exc.reason == "max_repairs"
```

- **`repair="error_only"`** (default) continues the chat: the model's reply and just the validation error are added to `messages=`. **`"full"`** restates the previous reply and the error in a single user turn. Both also work for a `prompt=` string; for other call shapes pass `reask=(args, kwargs, previous, error) -> (args, kwargs)`.
- **Hard caps.** There are never more than `1 + max_repairs` calls. `max_tokens=` stops repairing once the attempts' reported usage reaches it, and `budget=` (a `Budget`, with `budget_key=` for per-key budgets) stops it when the budget is exhausted. `ValidationRepairExhausted` carries `attempts`, `tokens`, `reason` (`max_repairs`, `max_tokens` or `budget`) and `last_error`.
- **Reply text** is read from a string, `.text`, `.content` blocks, `.choices[0].message.content`, `.output_text` or a dict of those; pass `text=` for anything else. A surrounding markdown fence is stripped (`unfence=False` to keep it).
- Exceptions from the call itself, such as `BudgetExceeded`, propagate untouched. Works for `async def` too. Events: `llm.validation_failed`, `llm.repair_exhausted`.

## Cassettes: record once, replay offline

A `Cassette` works at the HTTP transport, below any provider SDK. Give the client `transport=cassette.transport()` (most SDKs accept an `http_client=httpx.Client(transport=...)`). The first run talks to the real API and writes a JSON file; later runs replay it with no network and no key.

```python
import json
import os
import tempfile

from python_wrap.llm_cassette import Cassette, CassetteMiss, build_response


class Request:  # an httpx request has these attributes too
    method, url = "POST", "https://llm.example/v1/chat"
    headers = {"authorization": "Bearer sk-example-0123456789abcdef"}

    def __init__(self, prompt: str) -> None:
        self.content = json.dumps({"prompt": prompt}).encode()


class RealNetwork:  # stands in for httpx.HTTPTransport()
    def handle_request(self, request):
        body = b'{"text": "hi", "usage": {"input_tokens": 3}}'
        return build_response(request, 200, [("content-type", "application/json")], [body], asynchronous=False)


path = os.path.join(tempfile.mkdtemp(), "chat.json")
recorded = Cassette(path).transport(RealNetwork()).handle_request(Request("hello"))
assert recorded.read().startswith(b"{")

replay = Cassette(path, mode="replay").transport()  # no network object at all
assert replay.handle_request(Request("hello")).status_code == 200
assert "sk-example" not in open(path).read()  # secrets are never stored

try:
    replay.handle_request(Request("something new"))
except CassetteMiss as exc:
    assert "cannot be served" in str(exc)
```

Modes: `once` (default; replay if the file exists, otherwise record), `replay` (never touch the network; unrecorded requests raise `CassetteMiss`; use it in CI), `record` (start over), `new` (replay what is recorded, record the rest).

What a cassette guarantees:

- **Secrets are never stored.** Authorization-style headers and cookies, JSON keys such as `api_key`, `password` or `access_token`, query parameters such as `key` or `token`, the literal values in `secrets=[...]` and well-known key shapes (bearer tokens, `sk-...`, cloud keys, JWTs) are replaced by `***` before the file is written, including a secret split across stream chunks. Ordinary LLM fields such as `max_tokens` and `usage.input_tokens` are left alone. Supply `redactor=` to replace the key-based masking and `redact_responses=True` to apply it to JSON responses too. Requests are sent with `accept-encoding: identity` so bodies are readable and maskable. The file is written atomically with owner-only permissions.
- **Matching is by hash** of the method, the URL (query sorted) and the JSON body (keys sorted), computed from the masked form, so a changed API key still matches. Drop volatile fields with `ignore_body_keys=("user", "metadata.run_id")`, `ignore_query=`, `ignore_host=True`, and add `match_headers=` when a header matters. The same request recorded twice replays in order (a 429 followed by a 200 replays as 429, then 200); `allow_repeat=True` serves the last recording again instead of raising.
- **Streams keep their chunks.** A streamed body is recorded as it is consumed and replayed with the same chunk boundaries. A stream closed early is flagged incomplete and raises `CassetteError` if a replay reads past that point.
- **Damage is loud.** A truncated, corrupt or unknown-format file raises `CassetteError`; it is never half-used.

`RecordingTransport` and `ReplayTransport` are duck-typed against the `httpx` transport protocol (sync and async); httpx is only imported lazily to build real `httpx.Response` objects.

### Pytest fixtures

The plugin is opt-in. Add it to a top-level `conftest.py`:

<!-- skip -->
```python
pytest_plugins = ["python_wrap.pytest_plugin"]
```

It is not registered through the `pytest11` entry point on purpose: installing the library must not change how an unrelated test suite starts.

<!-- skip -->
```python
def test_summary(llm_cassette, chaos_llm_http):
    cassette = llm_cassette()  # cassettes/<test module>/test_summary.json
    client = make_client(transport=cassette.transport())
    ...
```

`llm_cassette(name=None, **options)` builds a `Cassette` named after the test and saves it at teardown. Choose the mode with `--llm-cassette-mode=replay` (or `PYTHON_WRAP_CASSETTE_MODE`) and the folder with `--llm-cassette-dir`. `chaos_llm_http` is a fresh `LLMHTTPFaults` on a private injector.

## HTTP-level chaos

`chaos.llm_http` reuses the `LLMFaults` rules but delivers them as HTTP. Wrap any transport (a real one, `httpx.MockTransport`, a `ReplayTransport`) and the SDK on top sees what a flaky API produces, so its own error mapping and your retries are tested together.

```python
from python_wrap.chaos import FaultInjector
from python_wrap.llm_cassette import build_response
from python_wrap.llm_http_chaos import LLMHTTPFaults


class Request:
    method, url, headers, content = "POST", "https://llm.example/v1", {}, b"{}"


class Network:
    def handle_request(self, request):
        return build_response(request, 200, [], [b'{"text": "hello world"}'], asynchronous=False)


faults = LLMHTTPFaults(FaultInjector())  # chaos.llm_http is the same, on the global injector
wire = faults.transport(Network())

with faults.rate_limit(times=1, retry_after=3):
    response = wire.handle_request(Request())
assert response.status_code == 429 and response.headers["retry-after"] == "3"

with faults.truncate(keep=0.5, times=1):
    assert len(wire.handle_request(Request()).read()) == 11  # half of 22 bytes
assert wire.handle_request(Request()).status_code == 200
```

| Rule | Effect on the wire |
| --- | --- |
| `rate_limit(retry_after=)` | a real 429 response with `Retry-After` |
| `overloaded()` / `server_error(status=)` | a 529 / 5xx response |
| `timeout(after=)` | `httpx.ReadTimeout` (a `TimeoutError` without httpx), optionally after a delay |
| `stall(seconds=, after_chunks=)` | hang before the response, or after N stream chunks |
| `truncate(keep=)` | the real response with its body cut to a fraction |

Rules accept `rate=` and `times=` like every chaos rule, wait through the library clock (so `fake_clock` skips stalls), and nothing is injected unless a rule was added.
