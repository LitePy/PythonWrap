# LangChain chat models

`guard_chat_model` puts python-wrap around a LangChain chat model without changing how
you call it. The guarded model has the same `invoke`, `ainvoke`, `stream` and `astream`
calls. Every call goes through one retry loop, a spend `Budget`, a token rate limit that
follows the provider's rate-limit headers, and streams that are reopened only when that
is safe.

python-wrap never imports LangChain. Install the extra, which pins the `langchain-core`
versions the adapter is tested against:

```bash
pip install "python-wrap[langchain]"
```

| Problem | Tool |
| --- | --- |
| python-wrap retries x LangChain and SDK retries | `guard_chat_model(model, ...)`, `without_retries(model)` |
| LangChain builds the SDK client, so `guard_openai` cannot be used | `guard_chat_model(model, ...)` |
| A chain or model you cannot wrap still needs observing | `PythonWrapCallback()` |

## Why not a plain retry

LangChain chat models retry by themselves. `max_retries` on the model and
`.with_retry()` on a runnable both add attempts on top of the provider SDK's own
retries. A python-wrap retry around them multiplies those attempts, and its
`RetryBudget` never sees the waits.

`guard_chat_model` removes the extra layers. It rebuilds the model from the fields you
set plus `max_retries=0`, so the provider client is created without SDK retries and
python-wrap owns the retry loop. The original model is unchanged.

## Quick start

<!-- skip -->
```python
from langchain_openai import ChatOpenAI
from python_wrap.adapters import guard_chat_model
from python_wrap.llm import Budget, TokenRateLimiter

model = guard_chat_model(
    ChatOpenAI(model="gpt-4o-mini", include_response_headers=True, stream_usage=True),
    budget=Budget(tokens=200_000, per="day"),
    limiter=TokenRateLimiter(tokens_per_minute=90_000),
    attempts=4,
)
reply = model.invoke("Summarise the release notes")
```

`include_response_headers=True` lets the limiter read the provider's rate-limit headers
from `response_metadata["headers"]`. `stream_usage=True` makes a stream report its usage
on the last chunk, so the budget can be charged.

## Options

`guard_chat_model(model, *, budget=None, limiter=None, policy=None, attempts=4,
retry=None, trace=False, first_token=None, chunk_gap=None, total=None)` takes:

| Option | Does |
| --- | --- |
| `budget=` | A `Budget`. Checked before each call and charged once, with the final `usage_metadata`. |
| `limiter=` | A `TokenRateLimiter`. Reserves an estimate per attempt and settles with the real usage. |
| `policy=` | Your per-attempt decorator, for example a timeout or a circuit breaker. Keep retries out of it. |
| `attempts=` | How many attempts the python-wrap retry loop makes. |
| `retry=` | Extra retry options, such as `delay`. See [Retry](../resilience/retry.md). |
| `trace=` | Emits one `genai.llm_call` event per call. See [observability](observability.md). |
| `first_token=`, `chunk_gap=`, `total=` | Stream timing limits, used by the stream wrapper. |

The guard returns a `GuardedChatModel`. A `.with_retry()` wrapper raises `ValueError`
because it would add its own retries. Unwrap it first and pass the model it wraps.

## What a guarded call goes through

Outermost first, for every `invoke` and `ainvoke`:

1. **Tracing** (`trace=`): one `genai.llm_call` event per call.
2. **`budget=`**: refused with `BudgetExceeded` before the request when the scope is
   spent. Charged once afterwards with the final `usage_metadata`, so a retried call
   costs what the answer cost, not each attempt.
3. **Retries**: python-wrap's retry loop, `attempts=` attempts with the `retry=`
   options.
4. **`policy=`**: your per-attempt decorator, run once per attempt.
5. **`limiter=`**: reserves an estimate per attempt, settles with the real usage and
   refunds a failed attempt. When the provider's headers carry the remaining tokens,
   the bucket is lowered to match.

The SDK client of the rebuilt model makes one HTTP request per attempt. The two layers
never retry the same request.

## Rebuilding the model

`guard_chat_model` also accepts a `bind_tools(...)` binding around a model. The model
under it is rebuilt, and the binding is kept.

If you only need the rebuild, call `without_retries(model)`. It returns the same model
with `max_retries=0` and no python-wrap layers.

Only `invoke`, `ainvoke`, `stream` and `astream` are guarded. Every other attribute,
such as `batch` or `bind_tools`, belongs to the retry-free model and is not guarded.
Use the guarded model for the calls that must be counted and retried.

## Streams

`stream` and `astream` return a resumable stream. It is reopened on a transient error
only before any chunk was handed out. After that, a failure raises
`StreamInterrupted`, because opening the stream again would repeat text or tool calls.

The usage on the last chunk's `usage_metadata` is charged when the stream ends, even
when it is interrupted. Use `first_token=`, `chunk_gap=` and `total=` to bound how long
the stream may stall. See [SDK clients](openai-anthropic.md#streams-that-never-replay-a-tool-call)
for the stream rules, which are the same here.

## Observe without wrapping

When the model cannot be wrapped, for example inside a chain you do not build, add
`PythonWrapCallback` to the callbacks. It only observes: tokens, latency, model and
errors go into the `genai.llm_call` event. It captures no content and never changes a
call.

<!-- skip -->
```python
from python_wrap.adapters import PythonWrapCallback

result = chain.invoke(x, config={"callbacks": [PythonWrapCallback()]})
```

`PythonWrapCallback(name="langchain")` sets the name the event carries. Use the default
unless you run several chains and need to tell them apart.

## Testing

The adapter is tested against langchain-core's real `BaseChatModel`. Two 429 responses
followed by a success make three provider calls, and the budget is charged once.
