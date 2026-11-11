# Examples

Runnable, standalone scripts that mirror the [cookbook](../docs/cookbook/index.md) recipes. They need only python-wrap (no third-party packages), print what they do, and finish in well under a second. Each one asserts its own results, so a clean exit means the behaviour described in the docstring really happened.

```console
$ pip install python-wrap
$ python examples/resilient_http.py
```

| Script | Shows | Recipe |
| --- | --- | --- |
| [`resilient_http.py`](resilient_http.py) | Retry with backoff and jitter, circuit breaker per host, rate limit, overall timeout, against a fake transport | [Resilient HTTP client](../docs/cookbook/resilient-http-client.md) |
| [`async_crawler.py`](async_crawler.py) | `map_async`, a `Bulkhead` per host, `race` over mirrors, `run_sync` | [Async crawler](../docs/cookbook/async-crawler.md) |
| [`web_caching.py`](web_caching.py) | `memoize(ttl=)`, `singleflight` against thundering herds, per-user `rate_limit` (framework-free) | [FastAPI](../docs/cookbook/fastapi.md) |
| [`service_layer.py`](service_layer.py) | Cached service functions, `transactional` commit/rollback hooks, `deprecate` | [Django](../docs/cookbook/django.md) |
| [`cli_tools.py`](cli_tools.py) | `timing`, `profile`, `benchmark`, `run_sync`, `debounce`, `throttle` | [CLI tools](../docs/cookbook/cli-and-scripts.md) |
| [`cooperative_timeout.py`](cooperative_timeout.py) | `checkpoint()`, `cancel_requested()` and `current_token()` stopping a sync function that `timeout` cannot interrupt; `foreign_alarm_state()` | [Timeout](../docs/features/resilience/timeout.md#cooperative-cancellation) |
| [`dataloader.py`](dataloader.py) | `batch_calls` turning 20 queries into 1 | [DataLoader batching](../docs/cookbook/dataloader-batching.md) |
| [`per_instance_state.py`](per_instance_state.py) | `throttle`/`debounce`/`rate_limit` with `per_instance=` and `key=`, `limiter_of(instance=)`, `memoize(singleflight=True)` with threads | [FastAPI](../docs/cookbook/fastapi.md#per-instance-state-in-service-classes) |
| [`audit_logging.py`](audit_logging.py) | `log_args` and `audit` with shared redaction, JSON log lines | [Observability](../docs/cookbook/observability.md) |
| [`llm_cassettes.py`](llm_cassettes.py) | A SQLite-shared token window with header-driven back-off, `validated` repairs and an HTTP `Cassette` with HTTP-level chaos, all offline | [Shared limits and cassettes](../docs/features/ai/shared-limits-and-cassettes.md) |
| [`llm_chaos.py`](llm_chaos.py) | `fake_llm` + `chaos.llm` faults + `fake_clock`: an agent survives rate limits, overload and broken JSON in milliseconds | [Testing agents](../docs/features/ai/testing-agents.md) |
| [`durable_buffer.py`](durable_buffer.py) | `Buffered(journal=SQLiteJournal(...))`: records survive a failed destination and a restart, then `replay()` delivers them | [Durable writers](../docs/features/concurrency/buffered-writes.md#durable-writers) |
| [`agent_tools.py`](agent_tools.py) | `@tool` replay-safe side effects, `RunGuard` loop/step limits, `ToolPolicy` dry run, approvals, `ToolRegistry` dispatch, PW017 | [Agent tool safety](../docs/features/ai/agent-safety.md) |
| [`llm_client.py`](llm_client.py) | `llm_client` retrying 529 after `Retry-After` but not refusals, per-user dollar `Budget`, `token_rate_limit`, `fallback_models`, `stream_timeout`, `llm_cache` against a fake provider | [LLM calls](../docs/features/ai/llm-calls.md) |
| [`strict_validation.py`](strict_validation.py) | `enforce_types(strict=True)`, `TypedDict` checking, `UnsupportedAnnotationError`, doctor `multiprocess=True` (PW012) | [Validation](../docs/features/correctness/validation.md#supported-annotations) |
| [`agent_audit_trail.py`](agent_audit_trail.py) | `traced_llm`/`traced_tool`, redacted `AuditTape`, retry attempt linkage, `replay` with mismatch detection | [AI observability](../docs/features/ai/observability.md) |
| [`resilient_llm_client.py`](resilient_llm_client.py) | `llm_client` retry after `Retry-After`, `Budget` refusal, `fallback_models`, `llm_cache` with `fake_llm` | [Resilient LLM client](../docs/cookbook/resilient-llm-client.md) |
| [`agent_tool_layer.py`](agent_tool_layer.py) | `tool` replay safety, approvals, `agent_run(budget=Budget)`, `traced_*` and `AuditTape` | [Agent tool layer](../docs/cookbook/agent-tool-layer.md) |
| [`mcp_client_demo.py`](mcp_client_demo.py) | Spawns `python -m python_wrap mcp`, performs the MCP handshake, lists tools, runs the `doctor` tool on two sample policies and asks for a recipe | [Agent-friendly tooling](../docs/features/ai/agent-friendly.md#the-mcp-server) |
| [`projects/etl_pipeline/`](projects/etl_pipeline/README.md) | A multi-file nightly pipeline: `process_batches` with checkpoint and resume, a `Bulkhead` per source, `timeout` with `checkpoint()`, `idempotent`, `throttle(key=)`, `memoize`, Prometheus export, health, chaos in a test | [Example projects](../docs/cookbook/example-projects.md) |
| [`projects/llm_gateway/`](projects/llm_gateway/README.md) | A multi-file LLM gateway: per-tenant `Budget` and `token_rate_limit`, `fallback_models` with per-provider breakers, `llm_cache`, `stream_timeout`, a JSONL audit tape, `doctor` and the `python -m python_wrap` CLI | [Example projects](../docs/cookbook/example-projects.md) |
| [`sdk_adapters/`](sdk_adapters/) | `openai_chat.py`, `anthropic_messages.py`, `pydantic_ai_agent.py`: python-wrap around the real SDKs, lazy imports, SKIP without a key, `--offline` fakes | [Around the real SDKs](../docs/cookbook/sdk-adapters.md) |

Several scripts use `python_wrap.testing.fake_clock` so that backoff sleeps and cache expiry take no real time. That is a handy trick in your own scripts and tests too; see [Testing code that uses python-wrap](../docs/cookbook/testing-your-code.md).

`tests/test_examples.py` runs every script in this directory in a subprocess, so the examples cannot rot.

## Example projects

Multi-file applications with their own README and tests live in [`projects/`](projects/). See [Example projects](../docs/cookbook/example-projects.md).

| Project | Shows |
| --- | --- |
| [`projects/orders_service/`](projects/orders_service/) | An orders service: `http_client` policy, `memoize` + `singleflight`, `idempotent`, per-user `rate_limit`, a journaled `buffered` email outbox with `replay`, `HealthRegistry`, events, `doctor` |
| [`projects/support_agent/`](projects/support_agent/) | A support agent: `@tool` with approval and idempotency, `RunGuard`, `Budget`, `llm_client`, `fallback_models`, `traced_*` with `Redactor`, `AuditTape` replay, `chaos.llm`, `doctor` |

`tests/test_example_projects.py` runs each project's tests and `main.py` in a subprocess.
