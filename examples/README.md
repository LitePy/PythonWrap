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
| [`durable_buffer.py`](durable_buffer.py) | `Buffered(journal=SQLiteJournal(...))`: records survive a failed destination and a restart, then `replay()` delivers them | [Durable writers](../docs/features/concurrency/buffered-writes.md#durable-writers) |
| [`strict_validation.py`](strict_validation.py) | `enforce_types(strict=True)`, `TypedDict` checking, `UnsupportedAnnotationError`, doctor `multiprocess=True` (PW012) | [Validation](../docs/features/correctness/validation.md#supported-annotations) |
| [`agent_audit_trail.py`](agent_audit_trail.py) | `traced_llm`/`traced_tool`, redacted `AuditTape`, retry attempt linkage, `replay` with mismatch detection | [AI observability](../docs/features/ai/observability.md) |

Several scripts use `python_wrap.testing.fake_clock` so that backoff sleeps and cache expiry take no real time. That is a handy trick in your own scripts and tests too; see [Testing code that uses python-wrap](../docs/cookbook/testing-your-code.md).

`tests/test_examples.py` runs every script in this directory in a subprocess, so the examples cannot rot.
