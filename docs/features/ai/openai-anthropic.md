# OpenAI and Anthropic SDK clients

`guard_openai` and `guard_anthropic` put python-wrap around the official OpenAI and
Anthropic Python SDK clients without changing how you call them. The guarded client
has the same call surface (`chat.completions.create`, `responses.create`,
`messages.create`, `messages.stream`, `beta.messages.tool_runner`) and every call goes
through one retry loop, a spend `Budget`, a token rate limit that follows the provider's
rate-limit headers, and streams that are reopened only when that is safe.

python-wrap still has no dependencies: the adapters never import `openai` or
`anthropic`. They read clients, responses, stream events and errors by shape, so they
also work with fakes in your tests. Install the SDK you use yourself (the `openai` and
`anthropic` extras only pin versions the adapters are tested against).

| Problem | Tool |
| --- | --- |
| python-wrap retries x SDK retries = 12 requests per call | `no_sdk_retries(client)`, `llm_retry(owns_retries=True)`, doctor PW018 |
| One wrapper for budget, retries, token limit and tracing | `guard_openai(client, ...)`, `guard_anthropic(client, ...)` |
| Token limit that knows what the provider knows | rate-limit headers read through `with_raw_response` |
| A stream fails halfway through a tool call | `resume_safe(...)`, `StreamInterrupted`, doctor PW019 |
| Prompt caching silently stops working | `CacheGuard` |

## Quick start

<!-- skip -->
```python
from openai import OpenAI

from python_wrap import guard_openai
from python_wrap.llm import Budget, token_rate_limit

client = guard_openai(
    OpenAI(),
    budget=Budget(usd=5.0, prices={"model-a": (0.4, 1.6)}, period=86_400),
    limiter=token_rate_limit(200_000),
    attempts=4,
)
reply = client.chat.completions.create(model="model-a", messages=[...])
```

The same with Anthropic is `guard_anthropic(Anthropic(), ...)`, then
`client.messages.create(...)`. Async clients (`AsyncOpenAI`, `AsyncAnthropic`) are
detected from their class name and return awaitables, exactly like the SDK.

Everything below runs offline: a tiny fake shaped like the SDK client stands in for
the real one.

```python
from types import SimpleNamespace

from python_wrap import guard_anthropic
from python_wrap.llm import Budget, BudgetExceeded
from python_wrap.testing import fake_clock


class Overloaded(Exception):
    status_code = 529  # what anthropic.InternalServerError carries for "overloaded"


class Messages:
    def __init__(self) -> None:
        self.requests = 0

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.requests += 1
        if self.requests == 1:
            raise Overloaded
        usage = SimpleNamespace(input_tokens=600, output_tokens=200,
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(model="model-a", usage=usage, content=[])


class Anthropic:  # just the shape: max_retries, with_options, messages
    def __init__(self, messages: Messages | None = None, max_retries: int = 2) -> None:
        self.messages = messages or Messages()
        self.max_retries = max_retries

    def with_options(self, *, max_retries: int) -> "Anthropic":
        return Anthropic(self.messages, max_retries)


messages = Messages()
spend = Budget(tokens=1_000)
client = guard_anthropic(Anthropic(messages), budget=spend, attempts=3)
with fake_clock():
    client.messages.create(model="model-a", max_tokens=200, messages=[])

assert messages.requests == 2  # one 529, one success: the SDK copy did not retry too
assert spend.spent().tokens == 800
client.messages.create(model="model-a", max_tokens=200, messages=[])  # crosses the limit
try:
    client.messages.create(model="model-a", max_tokens=200, messages=[])
except BudgetExceeded:
    pass  # refused before the request is sent
else:
    raise AssertionError("the budget should be spent")
```

## One retry loop, not three

Both SDKs retry connection errors, 408, 409, 429 and 5xx twice by default
(`max_retries=2`). Wrap such a client in `llm_retry(max_attempts=4)` and every
python-wrap attempt makes up to three HTTP requests: **4 x 3 = 12** requests for one
call, with waits your `RetryBudget` never sees. Add a helper that also retries and it
is 4 x 3 x 3.

There are two ways out, and the guarded clients use both:

- **`no_sdk_retries(client)`** returns `client.with_options(max_retries=0)`: a copy
  that shares the connection pool, with the SDK's retry loop off. The original client
  is unchanged.
- **`llm_retry(owns_retries=True)`** makes that retry the only loop for the call: any
  `llm_retry` running inside it (in a helper, or inside a guarded client) makes a
  single attempt, and if it runs inside another `llm_retry` it makes a single attempt
  itself. The scope is carried by a context variable, so it follows `await` and tasks
  but not new threads.

```python
from python_wrap.llm import llm_retry
from python_wrap.testing import fake_clock


class Overloaded(Exception):
    status_code = 529


calls = []


@llm_retry(max_attempts=3)
def helper() -> str:  # retries on its own when called directly
    calls.append(1)
    raise Overloaded


@llm_retry(max_attempts=4, owns_retries=True)
def answer() -> str:
    return helper()


with fake_clock():
    try:
        answer()
    except Overloaded:
        pass
assert len(calls) == 4  # not 4 x 3
```

`guard_openai(..., owns_retries=False)` is the other choice: the SDK keeps its retries
and python-wrap makes one attempt per call. Either way exactly one layer retries.

Pass `client=` to `llm_retry` when you call an SDK client yourself; `doctor` then warns
(PW018) while that client still has SDK retries on:

```python
from python_wrap.doctor import doctor
from python_wrap.llm import Budget, llm_retry


class OpenAI:  # the shape of the SDK client: max_retries defaults to 2
    max_retries = 2


client = OpenAI()


@Budget(tokens=10_000)
@llm_retry(max_attempts=4, client=client)
def ask(prompt: str) -> str:
    return prompt


finding = next(f for f in doctor(ask).findings if f.code == "PW018")
assert "4 x 3 = 12" in finding.message
```

## What a guarded call goes through

Outermost first, for every guarded `create`:

1. **Tracing** (`trace=True`, or a dict of `traced_llm` options): one `genai.llm_call`
   event per call, see [AI observability](observability.md).
2. **`budget=`**: refused with `BudgetExceeded` before the request when the scope is
   spent; charged with `response.usage` afterwards. `per="key"` key functions receive
   the SDK keyword arguments (`key=lambda **kw: kw["user"]`).
3. **Retries**: `llm_retry(max_attempts=attempts, owns_retries=True)` on the copy with
   `max_retries=0`. Extra options go in `retry={"delay": ..., "max_delay": ...,
   "max_time": ..., "budget": RetryBudget(...), "retry_if": ...}`. Bad requests,
   authentication errors and content-policy refusals are never retried.
4. **`policy=`**: your per-attempt decorator, typically
   `policy(timeout=timeout(30), breaker=CircuitBreaker(...))`. Keep retries out of it.
5. **`limiter=`** (a `TokenRateLimiter`): reserves `estimate_tokens(...)` per attempt,
   settles with the real usage, then lowers the bucket to what the provider reports
   (below). A failed attempt is refunded.

Usage is read from the real response objects: OpenAI `usage.prompt_tokens` /
`completion_tokens` (Chat Completions) and `usage.input_tokens` / `output_tokens`
(Responses), Anthropic `usage.input_tokens` / `output_tokens` plus
`cache_read_input_tokens` and `cache_creation_input_tokens`.

Attributes the guard does not wrap (`models`, `embeddings`, `files`,
`messages.count_tokens`, `messages.batches`...) are the original client's, with its own
SDK retries. `guarded.raw` is the client you passed, `guarded.client` the copy calls go
through, and `guarded.doctor()` lints the per-call stack.

### Rate-limit headers

With `read_headers=True` (the default) calls go through the SDK's
`with_raw_response`, so the response headers are available. After each response the
limiter's bucket is lowered (never raised) to `x-ratelimit-remaining-tokens` (OpenAI),
`anthropic-ratelimit-tokens-remaining` or `anthropic-ratelimit-input-tokens-remaining`
(Anthropic): the provider counts every process sharing your key, the local bucket only
this one. A 429 with `retry-after` drains the bucket so the next reservation waits at
least that long. `seed_limiter(limiter, headers, retry_after=...)` does the same for
your own calls.

```python
from python_wrap.adapters import seed_limiter
from python_wrap.llm import token_rate_limit
from python_wrap.testing import fake_clock

with fake_clock():
    limiter = token_rate_limit(60_000)  # 1,000 tokens a second
    seed_limiter(limiter, {"anthropic-ratelimit-tokens-remaining": "2500"})
    assert limiter.available == 2500
    seed_limiter(limiter, retry_after=3)
    assert limiter.try_reserve(1) >= 3  # the next call waits out the 429
```

## Streams that never replay a tool call

Retrying a stream is not like retrying a call. If it fails before anything was
received, opening it again is invisible to the caller. If it fails after half an answer
or in the middle of a tool call, opening it again sends the caller the same text twice,
or produces a second, different tool call while the first one may already be running.

`resume_safe(factory)` (and `create(..., stream=True)` on a guarded client) returns a
`ResumableStream` that:

- yields the provider's own events unchanged;
- holds back events that carry no output yet (`message_start`, `response.created`, a
  role-only chat chunk), so a reopen never duplicates them;
- reopens the stream on a failure before the first output when the error is transient
  (`stream_transient`: 429/5xx, connection and read errors, an `overloaded_error` event
  inside the stream, a stall), waiting `delay` doubled each time or the `Retry-After`;
- raises **`StreamInterrupted`** once output was handed out, with `.text` (what
  arrived), `.tool_calls` (calls whose arguments were complete), `.pending` (calls cut
  off mid-way: never run these), `.usage` and the original error as `__cause__`.
  `StreamInterrupted` is not transient, so an outer `llm_retry` does not replay it
  either.

```python
from types import SimpleNamespace

from python_wrap.adapters import StreamInterrupted, resume_safe


def event(kind: str, **fields: object) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def tool_events() -> list[SimpleNamespace]:  # Anthropic message events
    block = SimpleNamespace(type="tool_use", id="toolu_1", name="refund", input={})
    delta = SimpleNamespace(type="input_json_delta", partial_json='{"order": "A-')
    return [
        event("message_start", message=SimpleNamespace(usage=None)),
        event("content_block_start", index=0, content_block=block),
        event("content_block_delta", index=0, delta=delta),
    ]


class Dropped(ConnectionError):
    pass


def stream_that_dies():
    yield from tool_events()
    raise Dropped("connection reset")


try:
    for _ in resume_safe(stream_that_dies, delay=0):
        pass
except StreamInterrupted as exc:
    assert exc.tool_calls == ()  # nothing complete: nothing safe to run
    assert exc.pending[0].name == "refund" and exc.pending[0].arguments == '{"order": "A-'
else:
    raise AssertionError("expected StreamInterrupted")
```

Events are understood in every shape the SDKs stream: OpenAI chat-completion chunks
(`choices[].delta.content` and `delta.tool_calls`), OpenAI Responses events
(`response.output_text.delta`, `response.function_call_arguments.delta`,
`response.output_item.done`, `response.completed`), Anthropic events
(`content_block_start`/`_delta`/`_stop`, `message_delta`), python-wrap's `FakeStream`
chunks and plain strings. `stream.text`, `stream.tool_calls`, `stream.usage` and
`stream.text_stream` (text pieces only, `for` or `async for`) work in all of them.

Stall limits are `stream_timeout`'s: `first_token=`, `chunk_gap=`, `total=` on
`resume_safe` or on the guard. A stall before the first output is reopened; a stall
after it raises `StreamInterrupted` caused by `StreamTimeout`.

On a guarded client:

- `create(..., stream=True)` returns the `ResumableStream` (await it on an async
  client). The budget is checked and the limiter reserved once per stream; the usage the
  stream reports is charged when it ends, even when it is interrupted. Chat Completions
  streams get `stream_options={"include_usage": True}` unless you pass
  `stream_options`, otherwise they report no usage.
- `with client.messages.stream(...) as stream:` (Anthropic, `async with` for async
  clients) yields the `ResumableStream`; the SDK stream's helpers such as
  `get_final_message()` stay reachable on it.

As a decorator, `@resume_safe(...)` turns a function returning a stream into one
returning a `ResumableStream` that calls the function again to reopen. The doctor flags
a `retry` around a `stream_timeout` stream without `resume_safe` (PW019).

## Tool runner

`client.beta.messages.tool_runner(...)` on a guarded Anthropic client returns the SDK's
runner wrapped so that every step (one model request plus the tools it asked for) is
checked against the budget before it starts, charged with its usage, rate limited and
fed to the `CacheGuard`. `runner.until_done()` returns the last message;
`runner.steps` counts them.

python-wrap never repeats a runner step: the step may already have run your tools. The
runner therefore uses the original client, whose SDK retries repeat only the HTTP
request. Put idempotency on the tools themselves (see
[Agent tool safety](agent-safety.md)).

## Prompt cache guard

Prompt caching (Anthropic `cache_control` breakpoints, OpenAI's automatic prefix cache)
only pays off when the prefix (tools, system prompt, leading messages) is byte for byte
identical on every call. A timestamp or a request id in the system prompt silently
turns every call into a full-price miss.

`CacheGuard` watches for that. Pass one as `cache_guard=` to a guard (or call
`observe(response, request)` yourself). It adds up `cache_read_input_tokens` /
`cache_creation_input_tokens` (Anthropic) or `prompt_tokens_details.cached_tokens`
(OpenAI) and:

- once `min_calls` calls with a cacheable prompt (`min_prompt_tokens`, 1024 by
  default) have a hit rate at or below `min_hit_rate`, warns once with
  `PromptCacheWarning` and emits `llm.prompt_cache_cold` with the likely reasons: values
  that change per call, a prefix that differed on every call, or (Anthropic) no
  `cache_control` breakpoint in the requests;
- as soon as a prefix contains a timestamp, time of day, Unix time, UUID or long random
  id, warns once and emits `llm.prompt_cache_volatile`. Dates alone are fine.

Only hashes of the prefix and the kind of value found are kept, never prompt text.

```python
import warnings

from python_wrap.adapters import CacheGuard, PromptCacheWarning

guard = CacheGuard(min_calls=2, min_prompt_tokens=100)
request = {
    "system": "Request 3f2b8c1e-9a7d-4e21-b0c4-5d6e7f8a9b0c. You answer billing questions.",
    "messages": [{"role": "user", "content": "Why was I charged twice?"}],
}
miss = {"usage": {"input_tokens": 3000, "output_tokens": 50,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 3000}}
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    guard.observe(miss, request)
    guard.observe(miss, request)
assert guard.volatile == ("uuid",)
assert [w.category for w in caught] == [PromptCacheWarning, PromptCacheWarning]
assert guard.stats().hit_rate == 0.0
```

## Doctor checks

| Code | Severity | Problem |
| --- | --- | --- |
| PW018 | warning | A python-wrap LLM retry wraps an SDK client whose own retries are on (`llm_retry(client=...)` with `max_retries > 0`): attempts multiply (4 x 3 = 12). Use `no_sdk_retries(client)` or a guarded client. |
| PW019 | warning | A retry wraps a streamed call (`stream_timeout`) without `resume_safe`: the retry covers only opening the stream, and replaying it after output started repeats text and half-done tool calls. |

## Testing

The adapters are tested three ways:

- unit tests with fakes that mirror the SDK objects (usage fields, `with_raw_response`
  wrappers with `headers` and `parse()`, the real stream event types, errors carrying
  `status_code` and `response.headers`), plus property tests for the budget and
  attempt bounds and the no-replay rule;
- `python_wrap.llm_testing`: `fake_llm(...)` exposes `chat.completions.create` and
  `messages.create` (and `.aio` for async), so `guard_openai(fake_llm([...]))` works,
  and `chaos.llm.rate_limit(...)` / `overloaded(...)` inject provider faults;
- contract tests marked `sdk` that drive the real `openai` and `anthropic` packages
  against an in-process mock HTTP transport. They are skipped when the SDKs are not
  installed: `pip install openai anthropic`, then `pytest -m sdk`.

See [`examples/sdk_adapters/guarded_clients.py`](https://github.com/LitePy/PythonWrap/blob/main/examples/sdk_adapters/guarded_clients.py)
for both guards, streaming and the cache guard together, offline.
