# Testing agents

Code that calls a model API fails in ways ordinary services do not: a 429 with a `Retry-After`, a 529 "overloaded", JSON cut off mid-object, a stream that goes quiet, a tool call with arguments that do not parse, a refusal. python-wrap gives you two test doubles for these, both with zero dependencies and no provider SDK:

- `fake_llm(...)` / `FakeProvider`: a deterministic, scripted model that records calls and token usage.
- `chaos.llm`: LLM-shaped fault rules on the [chaos injector](../../testing/chaos.md).

Combined with `fake_clock`, an agent test that "waits" minutes for backoff finishes in milliseconds.

## A scripted provider

```python
from python_wrap import fake_llm

llm = fake_llm(["first", "second"], usage=(10, 4))

reply = llm.complete(messages=[{"role": "user", "content": "hi"}], model="m")
assert reply.text == "first"
assert reply.usage.input_tokens == 10          # Anthropic style
assert reply.content[0].text == "first"        # content blocks
assert reply.choices[0].message.content == "first"  # chat-completions shape

llm.assert_called_with(messages=[{"role": "user", "content": "hi"}])
llm.assert_tokens(input_tokens=10, output_tokens=4)
assert llm.call_count == 1
```

Each call that reaches the provider consumes the next reply of the script. A reply can be:

| Reply | Result |
| --- | --- |
| a string | a `FakeResponse` with usage |
| an exception instance or class | raised |
| a `FakeResponse`, `FakeStream`, dict or any object | returned as is |
| a callable taking the recorded `FakeCall` | whatever it returns, by these same rules |

When the script runs out the provider raises `FakeProviderExhausted`; pass `default=` or `cycle=True` to change that.

Usage is `usage=(input, output)`, a callable `(call, text) -> (input, output)`, or omitted to estimate from text length. `style="openai"` switches the usage object to `prompt_tokens` / `completion_tokens`. Dict replies are read the same way, so `{"usage": {"input_tokens": 3, "output_tokens": 1}}` counts too. Totals are available as `llm.input_tokens`, `llm.output_tokens` and `llm.total_tokens`.

`assert_called_with(*args, **kwargs)` checks the most recent call: the keywords you pass must be present and equal, and others (`max_tokens`, say) are ignored. Calls are recorded in `llm.calls` as `FakeCall` objects (`kwargs`, `messages`, `prompt`, `stream`, `served`).

### Entry points

| Call | Use |
| --- | --- |
| `llm.complete(...)` / `llm(...)` | sync |
| `await llm.acomplete(...)` | async |
| `llm.stream(...)` | sync stream (`for chunk in ...`) |
| `await llm.astream(...)` | async stream (`async for chunk in ...`) |
| `llm.messages.create(...)`, `llm.chat.completions.create(...)` | SDK-shaped, sync; `stream=True` returns a stream |
| `llm.aio.messages.create(...)`, `llm.aio.chat.completions.create(...)` | SDK-shaped, async |

### Streaming and delays

A string reply is streamed word by word (`chunk_size=` words per chunk). Chunks expose `.text`, `.delta.text` and `.choices[0].delta.content`, and the last chunk carries `.usage`.

Delays are `delay=` (per non-streaming call), `first_token_delay=` and `chunk_delay=`. They wait through the package sleep, so under `fake_clock` they advance the fake clock instead of sleeping:

```python
from python_wrap import fake_llm
from python_wrap.testing import fake_clock

llm = fake_llm(["one two three"], first_token_delay=2.0, chunk_delay=0.5)

with fake_clock() as clock:
    text = "".join(chunk.text for chunk in llm.stream(prompt="go"))

assert text == "one two three"
assert clock.total_slept == 3.5   # 2.0 first token + 3 x 0.5
```

For uneven gaps build a `FakeStream` yourself and use it as a reply. `chunk_delay` can be a list with one entry per chunk, and `error=` / `error_after=` raise partway through:

```python
from python_wrap import FakeStream, fake_llm
from python_wrap.llm_testing import APITimeoutError
from python_wrap.testing import fake_clock

stream = FakeStream(["a", "b", "c"], chunk_delay=[0, 0, 60], error=APITimeoutError, error_after=2)
llm = fake_llm([stream])

got = []
with fake_clock() as clock:
    try:
        for chunk in llm.stream():
            got.append(chunk.text)
    except TimeoutError:
        pass

assert got == ["a", "b"]
assert clock.total_slept == 60.0
```

## LLM faults

`chaos.llm` adds rules to the chaos injector. The point names are the ones your fault points use; a `FakeProvider` is a fault point called `llm.<name>` (`llm.fake` by default, or set `point=`), so `"llm.*"` matches it. Rules take the usual `rate`, `times` and seeded randomness, and return a `Fault` handle that works as a context manager.

| Rule | What happens |
| --- | --- |
| `rate_limit(name, retry_after=1.0)` | raises `RateLimitError`, status 429, `Retry-After` header and `retry_after` attribute |
| `overloaded(name)` | raises `OverloadedError`, status 529 |
| `server_error(name, status=500)` | raises `InternalServerError`, any 5xx |
| `timeout(name, after=None)` | raises `APITimeoutError` (a `TimeoutError`), after waiting `after` seconds |
| `slow_first_token(name, delay=5.0)` | waits `delay` seconds, then calls through |
| `stalled_stream(name, after_chunks=1, stall=30.0, fail=False)` | answers with a stream that pauses mid-way, optionally ending in a timeout |
| `malformed_json(name, text=..., keep=0.6)` | answers with `text` cut to the first 60% (`stop_reason="max_tokens"`) |
| `invalid_tool_call(name, tool=..., arguments=...)` | answers with a tool call whose raw arguments do not parse (or a dict of wrong values) |
| `refusal(name, message=...)` | answers with a content-policy refusal (`stop_reason="refusal"`, `finish_reason="content_filter"`) |

Rules that raise a fresh exception per injection. Rules that answer replace the provider's result, so `times=2` means two calls get the faulty answer and the next one reaches your script. Calls hit by a fault are still recorded (`llm.call_count`) but not counted as `llm.served_count`, and they do not use up script replies or tokens.

The errors are look-alikes for the usual SDK errors. They carry `status_code`, `response.status_code` and `response.headers`, and have the familiar class names (`RateLimitError`, `OverloadedError`, `InternalServerError`, `APITimeoutError`, `APIConnectionError`; base `APIStatusError`). Classification that matches by class name or by status attribute treats them like the real ones; they live in `python_wrap.llm_testing`.

```python
import pytest

from python_wrap import fake_llm
from python_wrap.chaos import FaultInjector
from python_wrap.llm_testing import OverloadedError

chaos = FaultInjector(seed=1)            # or use the global python_wrap.chaos.chaos
llm = fake_llm(["fine"], injector=chaos)

with chaos.llm.overloaded("llm.*", times=1):
    with pytest.raises(OverloadedError) as caught:
        llm.complete(prompt="hello")
    assert caught.value.status_code == 529

assert llm.complete(prompt="hello").text == "fine"
```

### From `PW_CHAOS`

Rules can come from the environment for a game day. Name the fault in `llm` and put its options next to `name`, `rate` and `times`:

```python
import json

from python_wrap import fake_llm
from python_wrap.chaos import FaultInjector
from python_wrap.llm_testing import RateLimitError

chaos = FaultInjector()
rules = chaos.enable_from_env(
    environ={"PW_CHAOS": json.dumps([
        {"name": "llm.*", "llm": "rate_limit", "retry_after": 2, "times": 1},
        {"name": "llm.*", "llm": "malformed_json", "keep": 0.5, "rate": 0.1},
    ])}
)
assert len(rules) == 2

llm = fake_llm(["ok"], injector=chaos)
try:
    llm.complete()
except RateLimitError as exc:
    assert exc.retry_after == 2
```

Unknown fault kinds and options are rejected before any rule is installed.

## A millisecond-fast agent test

Put the three together: the provider answers, chaos breaks it on a schedule, and the fake clock absorbs every backoff and stream delay.

```python
import json
import time

from python_wrap import fake_llm, retry
from python_wrap.chaos import chaos
from python_wrap.llm_testing import OverloadedError, RateLimitError
from python_wrap.testing import fake_clock

llm = fake_llm(['{"answer": 42}'], usage=(20, 6))

@retry(max_attempts=5, delay=30.0, backoff=2, on=(RateLimitError, OverloadedError))
def agent(question: str) -> dict:
    return json.loads(llm.complete(messages=[{"role": "user", "content": question}]).text)

started = time.perf_counter()
with fake_clock() as clock:
    chaos.llm.rate_limit("llm.*", times=2)
    chaos.llm.overloaded("llm.*", times=1)
    try:
        assert agent("meaning of life?") == {"answer": 42}
    finally:
        chaos.clear()

assert clock.sleeps == [30.0, 60.0, 120.0]   # backoff, never actually waited
assert llm.call_count == 4 and llm.served_count == 1
llm.assert_tokens(input_tokens=20, output_tokens=6)
assert time.perf_counter() - started < 1.0
```

Tips:

- Wrap the block in `fake_clock()` before adding rules that wait (`slow_first_token`, `timeout(after=)`, `stalled_stream`) so nothing sleeps for real.
- Call `chaos.clear()` in a fixture teardown so rules never leak between tests.
- Use `rate=` with a seeded `FaultInjector(seed=...)` to test "one call in five fails" reproducibly.
- To test a timeout wrapper around a stall, `stalled_stream(stall=...)` plus `fake_clock` shows exactly how long the consumer would have waited via `clock.total_slept`.

A runnable version lives in `examples/llm_chaos.py`.
