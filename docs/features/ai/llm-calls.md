# Wrapping LLM calls

Calls to a hosted language model fail in their own ways. Providers return 429 and 529
"overloaded" under load and tell you how long to wait. Bad prompts and content-policy
refusals must never be retried. Every call costs money, streams can stall halfway, and
the same question is often asked twice. `python_wrap.llm` has a wrapper for each of
these. None of them imports a provider SDK: responses and errors are duck-typed, so they
work with any client, or with your own HTTP code.

| Problem | Wrapper |
| --- | --- |
| Retry only what is worth retrying, honour `Retry-After` | `llm_retry()`, `presets.llm_client()` |
| Cap tokens and dollars per user, run or agent loop | `Budget` / `@budget(...)` |
| Stay under a tokens-per-minute quota | `token_rate_limit(...)` |
| Fall back to another model when one is down | `fallback_models([...])` |
| Stop a stream that stalls | `stream_timeout(first_token=, chunk_gap=, total=)` |
| Do not pay twice for the same question | `llm_cache(...)` |

## How usage and errors are read

**Token usage** comes from any of:

- `response.usage.input_tokens` / `output_tokens` (Anthropic style; cache reads and
  writes are added to the input count, which over- rather than under-counts);
- `response.usage.prompt_tokens` / `completion_tokens` (OpenAI style);
- the same keys on a dict;
- a `usage=` callable you pass, returning a `Usage`, an `(input, output)` tuple or a
  usage mapping.

`read_usage(response)` does this and returns a `Usage` (or `None`).

**Errors** are classified by `llm_transient(exc)`, which reads `status_code` / `status`
on the exception or its `.response`, the error body's `type`/`code`, and class names:

| Retried (and a reason to fall back) | Never retried |
| --- | --- |
| 408, 409, 429, 529, every 5xx | every other 4xx: bad request, auth, not found, 422 |
| bodies typed `overloaded_error`, `rate_limit_error` | content-policy and refusal errors (`ContentFilter...`, `content_policy_violation`, `refusal`, `safety`), even with a 5xx status |
| `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `InternalServerError` class names | `CircuitOpenError`, `BudgetExceeded` |
| any `ConnectionError` or `TimeoutError` | anything else (your bugs) |

```python
from python_wrap.llm import llm_transient


class APIStatusError(Exception):
    def __init__(self, status_code: int, code: str | None = None) -> None:
        self.status_code = status_code
        self.body = {"error": {"code": code}}


assert llm_transient(APIStatusError(429))
assert llm_transient(APIStatusError(529))
assert not llm_transient(APIStatusError(400))
assert not llm_transient(APIStatusError(400, "content_policy_violation"))
```

## Retries: `llm_retry` and the `llm_client` preset

`llm_retry()` is a `retry` that retries only `llm_transient` errors. Waits double from
1 s (plus up to 0.5 s jitter) unless the provider sent `Retry-After` or `retry-after-ms`;
that wait is used exactly, capped at `max_delay` (60 s by default). A `RetryBudget`
(20 % of calls plus 5 retries per second) keeps an outage from multiplying your traffic.

`presets.llm_client(name)` bundles it into a `Policy`:

| Layer | Default |
| --- | --- |
| `timeout` | 60 s per attempt (raise it for long answers; use `stream_timeout` for streams) |
| `retry` | 4 attempts, transient errors only, waits 1, 2, 4 s or `Retry-After`, `max_time` 421 s |
| retry budget | `RetryBudget(ratio=0.2, min_per_second=5)`, or pass `budget=` to share one |
| `breaker` | opens after 5 transient failures for 60 s; refusals and bad requests are not counted |
| `limiter` | optional `spend=Budget(...)` and `tokens=token_rate_limit(...)` |

```python
from python_wrap.llm import Budget
from python_wrap.presets import llm_client
from python_wrap.testing import fake_clock

chat = llm_client("chat", spend=Budget(tokens=200_000, name="chat"))
calls = []


class Overloaded(Exception):
    status_code = 529
    headers = {"retry-after": "2"}


@chat
def complete(prompt: str) -> dict[str, object]:
    calls.append(prompt)
    if len(calls) == 1:
        raise Overloaded
    return {"model": "m-1", "usage": {"input_tokens": 12, "output_tokens": 30}}


with fake_clock() as clock:
    complete("Summarise this")
assert clock.sleeps == [2.0]  # the provider's Retry-After
assert chat.explain().findings == ()
```

Pass `idempotent=False` to leave the retry layer out (tool calls with side effects).

## Spend budgets

A `Budget` checks its counter **before** each call and refuses with `BudgetExceeded` once
a limit is reached; after a successful call it adds the response's tokens, priced with
`prices={"model": (usd_per_million_input, usd_per_million_output)}`. The call that
crosses the limit still returns its (already paid for) answer; the next one is refused.
Failed calls spend nothing.

Price lookup uses the longest key the model name starts with (`"m-large"` prices
`"m-large-2025-01"`), then `"*"`. With a `usd=` limit, a model without a price raises
`LookupError`, because silently counting it as free would defeat the budget.

The `per=` scope picks the counter:

| `per=` | One counter for |
| --- | --- |
| `"total"` (default) | the whole budget (each `period=` window, e.g. `86400` for daily) |
| `"key"` | each `key(*args, **kwargs)`: a user, a tenant, an API key |
| `"run"` | each `with budget.run():` / `async with budget.run():` block |
| `"chain"` | each outermost decorated call: decorate the agent loop and its LLM calls with the same budget |

```python
from python_wrap.llm import Budget, BudgetExceeded

per_user = Budget(
    usd=0.05,
    per="key",
    key=lambda user, prompt: user,
    prices={"m-": (3.0, 15.0)},
    name="per-user",
)


@per_user
def ask(user: str, prompt: str) -> dict[str, object]:
    return {"model": "m-1", "usage": {"input_tokens": 5_000, "output_tokens": 2_000}}


ask("ann", "first")  # $0.045
ask("ann", "second")  # $0.090: crosses the limit, answer returned
try:
    ask("ann", "third")
except BudgetExceeded as exc:
    assert exc.scope == "key" and exc.usd > 0.05
ask("bob", "first")  # bob has his own allowance
assert per_user.spent(key="ann").tokens == 14_000
```

An agent loop capped as a whole:

```python
from python_wrap.llm import Budget, BudgetExceeded

loop_budget = Budget(tokens=1_000, per="chain", name="agent")


@loop_budget
def step(prompt: str) -> dict[str, object]:
    return {"usage": {"input_tokens": 300, "output_tokens": 100}}


@loop_budget
def agent(goal: str) -> int:
    steps = 0
    try:
        while True:
            step(goal)
            steps += 1
    except BudgetExceeded:
        return steps


assert agent("plan a trip") == 3  # 400, 800, 1200 tokens, then refused
assert agent("again") == 3  # each top-level run gets a fresh allowance
```

Context variables carry a run into tasks (`asyncio.gather`), `hedge` and `run_in_thread`.
For streamed responses the usage arrives with the last chunk: call
`budget.record(final_usage)` yourself.

**Across processes**, pass `backend=RedisBudgetBackend(redis_client)` (any
`redis.Redis`-compatible client; one Lua script per update, so concurrent adds never lose
spend), or your own object with `add(key, tokens, usd, ttl)`, `get(key)` and
`delete(key)`. Without it each worker process keeps its own counters, which the doctor
reports as PW012 under `multiprocess=True`.

## Token rate limits

Providers limit tokens per minute, not only requests. `token_rate_limit(tpm)` is a token
bucket: each call **reserves** an estimate before it starts, then the reservation is
replaced by the real usage (refunding or charging the difference); a failed call is
refunded. The default estimate, `estimate_tokens`, counts about four characters per
token in `messages`/`prompt`/`input`/`system` plus `max_tokens`; pass `estimate=` (a
tokenizer, or a number) for precision.

```python
from python_wrap.errors import RateLimitExceeded
from python_wrap.llm import token_rate_limit
from python_wrap.testing import fake_clock

quota = token_rate_limit(10_000, block=False, name="provider-tpm")


@quota
def complete(*, messages: list[dict[str, str]], max_tokens: int) -> dict[str, object]:
    return {"usage": {"input_tokens": 1_000, "output_tokens": max_tokens}}


with fake_clock() as clock:
    complete(messages=[{"role": "user", "content": "x" * 400}], max_tokens=4_000)
    complete(messages=[{"role": "user", "content": "x" * 400}], max_tokens=4_000)
    try:
        complete(messages=[{"role": "user", "content": "x" * 400}], max_tokens=4_000)
    except RateLimitExceeded as exc:
        clock.advance(exc.retry_after)
    complete(messages=[{"role": "user", "content": "x" * 400}], max_tokens=4_000)
```

With `block=True` (the default) the call waits instead, up to `max_wait=` seconds, and
emits `rate_limit.waited`. One limiter instance can decorate every function that shares
the provider quota.

## Model fallback chains

`fallback_models([call_a, call_b, ...])` tries each entry in order. Each sits behind its
own circuit breaker (5 transient failures open it for 30 s), so a provider that is down
is skipped at once. Transient errors and open breakers move on to the next entry;
refusals and bad requests are raised, so one model's refusal is not hidden behind
another model's answer. Change that with `on=` (exception types or a predicate). Every
answer emits `llm.fallback_served` naming the model that served it.

```python
from python_wrap.events import listening
from python_wrap.llm import fallback_models


class Unavailable(Exception):
    status_code = 503


def large_model(prompt: str) -> str:
    raise Unavailable


def small_model(prompt: str) -> str:
    return f"small model answer to {prompt!r}"


ask = fallback_models([large_model, small_model], names=["large", "small"], cached_answer=True)

served = []
with listening(lambda event: served.append(event.fields["model"])):
    ask("hello")
assert served == ["small"]
```

The decorator form makes the decorated function the primary:
`@fallback_models(backups=[small_model])`. Pass `breaker={"name": CircuitBreaker(...)}`
to configure breakers yourself (the same instance under two names shares one breaker per
provider), or `breaker=False`. With `cached_answer=True` (or a `CacheStore`), the last
good answer for the same arguments is served, as model `"cached"`, when every entry fails.

## Streaming timeouts

A plain `timeout` around a streaming call only covers opening the stream. `stream_timeout`
limits the waits while you iterate:

- `first_token`: until the first chunk (from the call in decorator form);
- `chunk_gap`: between later chunks;
- `total`: for the whole stream.

When one trips, the stream is closed and `StreamTimeout` (a `TimeoutExpired`) is raised
with `.limit` set to `"first_token"`, `"chunk_gap"` or `"total"`.

```python
import asyncio

from python_wrap.llm import StreamTimeout, stream_timeout


@stream_timeout(first_token=1, chunk_gap=0.05, total=30)
async def stream_answer(prompt: str):
    yield "The answer"
    yield " is"
    await asyncio.sleep(1)  # the provider stalls
    yield " 42"


async def main() -> list[str]:
    received = []
    try:
        async for chunk in stream_answer("question"):
            received.append(chunk)
    except StreamTimeout as exc:
        assert exc.limit == "chunk_gap"
    return received


assert asyncio.run(main()) == ["The answer", " is"]
```

It wraps sync and async iterators, generators, and SDK stream objects (their other
attributes stay reachable): `stream_timeout(client_stream, chunk_gap=10)`. Sync streams
are read one chunk ahead in a daemon thread so the wait can time out; a read already
blocked there ends when its connection does, and sources with `close()` (HTTP streams)
are closed to abort it. Limits run on real time.

Retries do not cover a stream that fails halfway: `llm_retry` retries opening it, and
after that the partial answer is yours to keep or discard.

## Response cache

`llm_cache()` caches answers keyed on the request: the SHA-256 of the call's arguments
(model, messages or prompt, parameters; `**kwargs` flattened, dict order ignored,
pydantic models and dataclasses dumped), minus transport options (`stream`, `timeout`,
`extra_headers`, `user`, `metadata`, `request_id`; change with `ignore=`). Calls with
`stream=True`, failures and `None` answers are never cached. Entries expire after `ttl`
seconds (default one hour). The default store is an in-process LRU of `maxsize` entries;
pass any `CacheStore` (such as `RedisStore`) or, for `async def` functions, an
`AsyncCacheStore`.

For semantic matches, pass `similarity=embed`, a function turning the prompt text into a
vector (your own embedding call). On an exact miss the prompt is compared, by cosine
similarity, with recent prompts sent to the **same** model with the same parameters, and
an answer at least `threshold` (0.95) similar is reused.

```python
from python_wrap.llm import llm_cache

calls = []


def embed(text: str) -> list[float]:  # stands in for a real embedding model
    words = text.lower().strip("?!. ").split()
    return [float("weather" in words), float("paris" in words), float("london" in words)]


@llm_cache(ttl=600, similarity=embed, threshold=0.9)
def complete(*, model: str, prompt: str, temperature: float = 0.0) -> str:
    calls.append(prompt)
    return "Mild, with showers."


complete(model="m-1", prompt="Weather in Paris?")
complete(model="m-1", prompt="Weather in Paris?", temperature=0.0)  # exact hit
complete(model="m-1", prompt="weather in paris")  # semantic hit
complete(model="m-1", prompt="Weather in London?")  # different question
assert calls == ["Weather in Paris?", "Weather in London?"]
```

Cache only deterministic requests (temperature 0) or answers you are happy to reuse.
`disable_caching()` turns the cache off in tests; hits and misses emit `cache.hit` and
`cache.miss`.

## Doctor checks

`Policy.explain()`, `doctor(func)` and `python -m python_wrap doctor` recognise these
wrappers and add three checks to the [list](../resilience/presets-and-doctor.md):

| Code | Severity | Problem |
| --- | --- | --- |
| PW013 | warning | An LLM call is retried (`llm_retry`, `llm_client`) but has no `Budget`: a retry storm or agent loop spends without limit. |
| PW014 | warning | A stack with LLM layers has no timeout, or a `stream_timeout` has no `chunk_gap`. |
| PW015 | warning | A generic `retry` without `retry_if=` wraps an LLM call: bad requests and refusals are retried and billed again. |

```python
from python_wrap import retry, timeout, policy
from python_wrap.llm import Budget
from python_wrap.presets import llm_client

assert [f.code for f in llm_client("chat").explain().findings] == ["PW013"]

naive = policy(limiter=Budget(tokens=10_000), retry=retry(max_attempts=3), timeout=timeout(30))
assert "PW015" in [f.code for f in naive.explain().findings]
```

## Recommended stack

Outermost first: `llm_cache` (a hit costs nothing), `fallback_models` across providers,
then per provider `llm_client(name, spend=Budget(...), tokens=token_rate_limit(...))`,
and `stream_timeout` on streaming calls. See
[`examples/llm_client.py`](https://github.com/LitePy/PythonWrap/blob/main/examples/llm_client.py)
for all of them together against a fake provider.
