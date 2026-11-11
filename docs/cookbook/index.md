# Cookbook

The guides explain one decorator at a time. The cookbook shows how they combine to solve problems you actually have. Each recipe has the same shape:

1. **The problem**, in terms of what goes wrong in production.
2. **The code**, complete and runnable. Every code block on these pages is executed by the docs test suite, so what you read works.
3. **How it works**, one decorator at a time.
4. **Pitfalls**, the mistakes that cost a day.
5. **Test it**, a runnable test file that uses [`python_wrap.testing`](../testing/index.md), so the decorators never make your tests slow or flaky.

Every recipe that matters has a standalone script in the [`examples/`](https://github.com/LitePy/PythonWrap/tree/main/examples) directory that you can run with `python examples/<name>.py`. No third-party packages are needed; framework code (FastAPI, Django, httpx) is shown but marked as such.

## Recipes

| Recipe | Decorators and helpers | You get |
| --- | --- | --- |
| [A resilient HTTP client](resilient-http-client.md) | `retry`, `CircuitBreaker`, `rate_limit`, `timeout` | Retries with backoff and jitter, a circuit breaker per host, a request budget, an overall deadline |
| [An async crawler and bulk fetch](async-crawler.md) | `map_async`, `Bulkhead`, `race`, `run_sync`, `amap_iter` | Bounded concurrency, per-host limits, collected errors, mirror racing |
| [FastAPI](fastapi.md) | `memoize(ttl=)`, `singleflight`, `rate_limit(key=)` | Cached endpoints, no thundering herd, per-user limits and `429` responses |
| [Django](django.md) | `memoize`, `transactional`, `deprecate` | Cached services with explicit invalidation, commit hooks and undo steps, painless renames |
| [CLI tools and scripts](cli-and-scripts.md) | `timing`, `profile`, `benchmark`, `run_sync`, `debounce`, `throttle` | Step timings, profiles, honest benchmarks, file watchers that rebuild once |
| [DataLoader-style batching](dataloader-batching.md) | `batch_calls` | N+1 queries collapsed into one, with per-item calling code |
| [Testing code that uses python-wrap](testing-your-code.md) | `fast_mode`, `fake_clock`, `spy`, `disable_*` | Fast, deterministic tests with no sleeping and no leaked state |
| [Durable buffered writes](../features/concurrency/buffered-writes.md#durable-writers) | `buffered(journal=)`, `SQLiteJournal`, `replay()` | Writes that survive a crash and are replayed after restart (`examples/durable_buffer.py`) |
| [Strict validation](../features/correctness/validation.md#supported-annotations) | `enforce_types(strict=True)`, `TypedDict`, `doctor --multiprocess` | Typed boundaries that refuse what they cannot check (`examples/strict_validation.py`) |
| [A resilient LLM client](resilient-llm-client.md) | `llm_client`, `Budget`, `fallback_models`, `llm_cache` | Transient-only retries, spend caps, a backup model, cached answers (`examples/resilient_llm_client.py`) |
| [A safe agent tool layer](agent-tool-layer.md) | `tool`, `agent_run`, `ToolPolicy`, `traced_tool`, `AuditTape` | Replay-proof tools, approvals, run budgets, a redacted audit tape (`examples/agent_tool_layer.py`) |
| [Building with AI coding agents](building-with-agents.md) | `llms.txt`, `python -m python_wrap api/recipe/guide/mcp`, `doctor` | A workflow for coding agents to adopt python-wrap correctly |
| [Observability](observability.md) | `log_args`, `audit`, `timing` | Redacted, structured logs and an audit trail |
| [Example projects](example-projects.md) | `http_client`, `idempotent`, `buffered(journal=)`, `ToolRegistry`, `AuditTape`, `doctor` | Two multi-file apps (an orders service and a support agent) with fakes, tests and replay, to read and copy |

## Reading order

New to python-wrap? Start with the [resilient HTTP client](resilient-http-client.md): it uses four decorators together and introduces the testing helpers. If you write web services, continue with [FastAPI](fastapi.md) or [Django](django.md). Whatever you build, finish with [Testing](testing-your-code.md).

## Ground rules the recipes follow

* **Stack deliberately.** The decorator closest to the function runs first. `@retry` above `@rate_limit` means every retry counts against the limit; the other way round, the whole retry loop counts once. See [Stacking decorators](../get-started/stacking.md).
* **State belongs to the function.** Caches, breakers and rate-limit windows are created once per decorated function and shared by every caller in the process. That is the point, and also the first thing to reset in tests.
* **Libraries do not configure logging.** Recipes that print or collect logs set up their own handlers, as your application should.
* **Be explicit about what is safe to repeat.** `retry`, `hedge` and `race` may run an operation more than once. Use them for reads and idempotent writes.

## Framework lifecycle

[Framework integrations](framework-integrations.md) cover Flask, FastAPI/ASGI, Django
and Celery request/task context, cooperative deadlines and explicit worker startup.
