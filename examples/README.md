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
| [`dataloader.py`](dataloader.py) | `batch_calls` turning 20 queries into 1 | [DataLoader batching](../docs/cookbook/dataloader-batching.md) |
| [`audit_logging.py`](audit_logging.py) | `log_args` and `audit` with shared redaction, JSON log lines | [Observability](../docs/cookbook/observability.md) |

Several scripts use `python_wrap.testing.fake_clock` so that backoff sleeps and cache expiry take no real time. That is a handy trick in your own scripts and tests too; see [Testing code that uses python-wrap](../docs/cookbook/testing-your-code.md).

`tests/test_examples.py` runs every script in this directory in a subprocess, so the examples cannot rot.
