# A resilient LLM client

## The problem

Calling a model API in production fails in ways ordinary HTTP does not: rate limits that tell you exactly how long to wait, "overloaded" errors that clear in seconds, refusals that must never be retried (you would pay for each), a month of spend that one bad loop can burn in an hour, and answers you pay for twice because the same question was asked twice.

The goal is one small client function with:

- retries for transient errors only, honouring the provider's `Retry-After`,
- a token or dollar budget that refuses calls once spent,
- a backup model when the primary is down,
- a cache for repeated deterministic questions.

No provider SDK is imported by python-wrap: responses and errors are read by shape (`usage.input_tokens` or `usage.prompt_tokens`, `status_code`). The code below uses `fake_llm`, so it runs anywhere; swap `provider.complete(...)` for your SDK call.

## The code

```python
from python_wrap import fake_llm
from python_wrap.llm import Budget, BudgetExceeded, fallback_models, llm_cache
from python_wrap.llm_testing import OverloadedError, RateLimitError
from python_wrap.presets import llm_client
from python_wrap.testing import fake_clock

provider = fake_llm([RateLimitError("slow down", status_code=429, retry_after=2.0), "hello"])
spend = Budget(tokens=500, name="chat-spend")
chat = llm_client("chat", spend=spend)


@chat
def ask(question: str):
    return provider.complete(messages=[{"role": "user", "content": question}])


with fake_clock() as clock:
    answer = ask("Say hello")
assert answer.text == "hello" and clock.sleeps == [2.0]  # waited what the provider asked
assert spend.spent().tokens > 0

# A backup model for outages.
primary = fake_llm([OverloadedError("busy")], cycle=True)
backup = fake_llm(["from the backup"], default="from the backup")
robust = fallback_models(
    [
        lambda q: primary.complete(messages=[{"role": "user", "content": q}]),
        lambda q: backup.complete(messages=[{"role": "user", "content": q}]),
    ],
    names=["primary", "backup"],
)
assert robust("status?").text == "from the backup"

# Cache deterministic questions.
cached_provider = fake_llm(["a definition"], default="a definition")


@llm_cache(ttl=60)
def define(*, model: str, messages: list[dict[str, str]]):
    return cached_provider.complete(messages=messages)


question = [{"role": "user", "content": "Define idempotent."}]
define(model="m-1", messages=question)
define(model="m-1", messages=question)
assert cached_provider.call_count == 1

# A budget refuses once it is spent.
tight = Budget(tokens=10, name="tight")


@tight
def spendy(prompt: str):
    return {"usage": {"input_tokens": 8, "output_tokens": 8}}


spendy("one")  # crosses the limit; the paid-for answer is still returned
try:
    spendy("two")
except BudgetExceeded:
    pass
else:
    raise AssertionError("expected a refusal")
```

## How it works

- `llm_client("chat", spend=...)` stacks the layers in the order the [doctor](../features/resilience/presets-and-doctor.md) expects: budget outermost, then the circuit breaker, token limit and retry, with a per-attempt timeout innermost. Retries use `llm_retry`: rate limits, overloads, 5xx, timeouts and connection errors only, `Retry-After` honoured, 4xx refusals fail at once and do not trip the breaker.
- `Budget` checks before each call and adds the response's usage after it. The call that crosses the limit still returns its answer; the next is refused with `BudgetExceeded`. Use `per="key"` for a per-user allowance and `period=86400` for a daily one.
- `fallback_models` keeps one breaker per model, so a dead primary is skipped quickly; an `llm.fallback_served` event names the model that answered.
- `llm_cache` keys on the model, messages and generation parameters. Cache only deterministic requests.

## Pitfalls

- Do not wrap an LLM call in a generic `retry` without `retry_if=`: refusals and bad requests get retried and paid for (doctor code PW015).
- A retried LLM call with no `Budget` is flagged by PW013: a retry storm is a spend storm.
- Streaming responses report usage with the last chunk: call `budget.record(final_usage)` yourself, and bound the stream with `stream_timeout(first_token=..., chunk_gap=...)`.
- Keep personal data out of shared caches.

## Test it

`fake_llm` plus `fake_clock` make the whole client testable in milliseconds, and `chaos.llm` injects rate limits, overloads, timeouts and broken JSON on demand; see [Testing agents](../features/ai/testing-agents.md). The runnable script is `examples/resilient_llm_client.py`; see also [LLM calls](../features/ai/llm-calls.md).
