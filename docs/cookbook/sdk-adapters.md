# Around the real SDKs

## The problem

The other recipes use fakes so they run anywhere. In an application you hold an `openai.OpenAI()`, an `anthropic.Anthropic()` or a pydantic-ai `Agent`. This page shows where python-wrap goes around each, and how to keep the code testable without the SDK installed.

python-wrap never imports a provider SDK. It reads what responses and errors look like:

- usage from `usage.input_tokens` / `usage.output_tokens` (Anthropic style) or `usage.prompt_tokens` / `usage.completion_tokens` (OpenAI style);
- retryability from `status_code` (or `response.status_code`): 408, 409, 429, 529 and 5xx are retried, other 4xx are not;
- the wait from the `retry-after` header on `error.response`.

So you wrap the SDK's *method*, not the SDK.

## The pattern

<!-- skip -->
```python
from python_wrap.llm import Budget
from python_wrap.presets import llm_client

spend = Budget(tokens=50_000, name="chat-spend")
policy = llm_client("chat", spend=spend, attempts=3)


@policy
def chat(**kwargs):
    return client.chat.completions.create(**kwargs)   # or client.messages.create(**kwargs)
```

The same function works with a fake that has the same response shape, which is what the runnable block below does (it stands in for `client.chat.completions.create`):

```python
from types import SimpleNamespace

from python_wrap.llm import Budget
from python_wrap.presets import llm_client
from python_wrap.testing import fake_clock


class Overloaded(Exception):
    status_code = 529
    response = SimpleNamespace(status_code=529, headers={"retry-after": "2"})


calls = []


def create(**kwargs):  # the SDK method, faked with the same shape
    calls.append(kwargs)
    if len(calls) == 1:
        raise Overloaded()
    message = SimpleNamespace(content="hi", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
    )


spend = Budget(tokens=1_000, name="docs-spend")
chat = llm_client("docs-chat", spend=spend)(create)
with fake_clock() as clock:
    reply = chat(model="any", messages=[{"role": "user", "content": "hello"}])
assert reply.choices[0].message.content == "hi"
assert clock.sleeps == [2.0]  # waited what the provider asked
assert spend.spent().tokens == 40
```

## The three scripts

Each lives in `examples/sdk_adapters/` in the repository, imports its SDK lazily inside `main()`, prints a `SKIP` notice and exits 0 when the SDK, the model name or the API key is missing, and has a `--offline` path that runs against a tiny fake with the real response shape. `tests/test_sdk_adapters.py` runs those offline paths. The SDKs are not dependencies of python-wrap.

| Script | Targets | Shows |
| --- | --- | --- |
| `openai_chat.py` | `openai>=1.40,<3` | `chat.completions.create` with `llm_client` and a token `Budget`; a tool loop on a `ToolRegistry` with a `RunGuard`; the Responses API under the same policy |
| `anthropic_messages.py` | `anthropic>=0.40,<1` | `messages.create` tool loop with `tool_use` / `tool_result` blocks; `messages.stream` guarded by `stream_timeout`, usage recorded with `Budget.record` |
| `pydantic_ai_agent.py` | `pydantic-ai>=1.0,<2` | Tools wrapped with retry, a `Quota` on a paid search and a replay-safe write; `agent.tool_plain` for the wrapped functions; `tools.run()` around `agent.run_sync` |

Run one for real by setting the model and key, for example:

<!-- skip -->
```console
$ OPENAI_API_KEY=... OPENAI_MODEL=<model id> python examples/sdk_adapters/openai_chat.py
$ python examples/sdk_adapters/openai_chat.py --offline
```

## Why these choices

- **Wrap the method, keep the client.** The SDK keeps its own HTTP retries; set `max_retries=0` on the client when `llm_client` owns retrying, or the two multiply.
- **Tools are the part with side effects.** A replayed or repeated model turn must not refund twice: `ToolRegistry.tool` derives an idempotency key (or takes `idempotent_key=` for a business key such as an order id).
- **Streams need their own limits.** A plain `timeout` ends when the stream *opens*. `stream_timeout(first_token=, chunk_gap=)` watches the iteration, and `Budget.record` adds the usage that arrives with the last chunk.
- **Fakes mirror shapes, not behaviour.** The offline fakes return objects with the attributes the code reads. If the SDK changes a shape, the live run (not the offline test) is what notices, so run the scripts for real when you upgrade an SDK.
