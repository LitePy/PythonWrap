# LLM gateway

An internal service every team calls instead of talking to model providers directly. It enforces each tenant's allowance, falls back between providers, caches repeated questions and writes an audit trail. Providers are fakes (`fake_llm`), so nothing touches the network and the tests run in about a second.

```console
$ python examples/projects/llm_gateway/run.py
$ python -m pytest examples/projects/llm_gateway/tests
$ cd examples/projects/llm_gateway && python -m python_wrap doctor gateway_app.app:acme_chat
```

## Layout

| File | Role |
| --- | --- |
| `run.py` | Entry point: a few requests, the doctor findings, the audit file |
| `gateway_app/config.py` | Tenant plans, price table, stream limits (all invented values) |
| `gateway_app/providers.py` | Two fake providers with a timeout and an audit trace on every call |
| `gateway_app/gateway.py` | The gateway: per-tenant stacks, fallback chain, streaming |
| `gateway_app/app.py` | Wiring, and a module-level handler for the doctor CLI |
| `tests/test_llm_gateway.py` | Happy path, budget, outage, stall, CLI surfaces |

## Which feature does what, and why

| Feature | Where | Why it is there |
| --- | --- | --- |
| `Budget(usd=..., prices=...)` per tenant | `gateway.py` | A tenant that spent its allowance is refused before the provider is called. The call that crosses the line is still answered, the next one is refused (`BudgetExceeded`) |
| `token_rate_limit` per tenant | `gateway.py` | Each tenant gets a share of the provider's tokens-per-minute quota. `block=False` raises instead of making the caller wait |
| `fallback_models` + `CircuitBreaker` per provider | `gateway.py` | If the first provider is overloaded the second answers. After three failures the first provider's breaker opens, so it is skipped instead of being tried (and timed out) on every request. Breakers are shared across tenants because an outage is everyone's outage |
| `llm_cache` | `gateway.py` | Outermost layer, so a repeated question costs no tokens, quota or money. One cache per tenant: answers never cross tenants |
| `timeout` | `providers.py` | A hung provider call cannot hold a request forever |
| `stream_timeout(first_token=, chunk_gap=)` | `gateway.py` | A stream that stalls mid-answer is cut with `StreamTimeout` instead of hanging the client |
| `Budget.record` | `gateway.py` | Streams bypass the decorator, so the usage on the last chunk is recorded by hand. The budget is also checked up front |
| `traced_llm` + `AuditTape` (JSONL) | `providers.py`, `app.py` | One line per real provider call with tokens, cost and latency. Cache hits write nothing because they never reach a provider |
| `doctor` | `gateway.py`, CLI | Prints findings about the stack. It reports PW006 here: the budget sits *outside* the fallback chain on purpose, so one logical request is charged once, with the usage of whichever provider answered |

## python-wrap CLI surfaces

`python -m python_wrap doctor gateway_app.app:acme_chat` explains the layers (cache, budget, limiter, fallback, timeout) and lints them. `python -m python_wrap api llm_cache` and `python -m python_wrap recipe limit llm tokens` answer "what is this" and "what do I use". The tests call the same entry point in-process.
