# API reference

These pages are generated from the docstrings, so they always match the code. Almost
everything is importable from the top-level `python_wrap` package; the exceptions are the
helpers that live in a submodule (`python_wrap.testing`, `python_wrap.events`, and
`remaining`/`check` in `python_wrap.deadline`).

For explanations and examples, start from the matching [feature page](../features/index.md).

## Resilience

| Module | Contents |
| --- | --- |
| [retry](retry.md) | `retry`, `retrying`, `retry_stats`, `retry_after_header`, `fallback`, `suppress`, `retry_on_exception`, `Attempt`, `RetryState`, `RetryStats`, `Retrying` |
| [circuit](circuit.md) | `CircuitBreaker`, `circuit_breaker`, `CircuitState`, `CircuitStats`, `CircuitProtected` |
| [deadline](deadline.md) | `deadline`, `remaining`, `check`, `Deadline` |
| [hedging](hedging.md) | `hedge` |
| [policy](policy.md) | `policy`, `Policy`, `ROLES` |

## Caching and batching

| Module | Contents |
| --- | --- |
| [caching](caching.md) | `memoize`, `cache`, `once`, `cached_property`, `invalidate`, `CacheInfo`, `Memoized`, `Once`, `CachedProperty` |
| [batching](batching.md) | `batch_calls`, `Batched`, `AsyncBatched` |

## Concurrency and limits

| Module | Contents |
| --- | --- |
| [concurrency](concurrency.md) | `timeout`, `Timeout`, `rate_limit`, `RateLimiter`, `Limit`, `limiter_of`, `throttle`, `debounce`, `singleflight`, `run_in_thread` |
| [aio](aio.md) | `gather_limit`, `map_async`, `amap_iter`, `race`, `RaceFailed`, `run_sync` |
| [bulkhead](bulkhead.md) | `Bulkhead`, `limit_concurrency`, `Limited` |
| [periodic](periodic.md) | `periodic`, `Periodic`, `AsyncPeriodic` |

## Observability

| Module | Contents |
| --- | --- |
| [logs](logs.md) | `log_args`, `log_return`, `trace`, `audit`, `AuditRecord`, `log_context`, `current_log_context`, `DEFAULT_REDACT` |
| [timing](timing.md) | `timing`, `benchmark`, `benchmark_async`, `measure_memory`, `profile`, `TimingRecord`, `BenchmarkResult`, `MemoryRecord` |
| [metrics](metrics.md) | `count_calls`, `CallStats`, `Counted` |
| [events](events.md) | `subscribe`, `listening`, `filter_events`, `Event`, `EVENT_NAMES` |

## Correctness

| Module | Contents |
| --- | --- |
| [validation](validation.md) | `validate_args`, `check_type`, `enforce_types` |
| [state](state.md) | `revert_on_failure`, `Transaction`, `transactional`, `INJECTED`, `current_transaction`, `require_transaction` |

## Everything else

| Module | Contents |
| --- | --- |
| [config](config.md) | `configure`, `configured`, `get_config`, `reset_config` |
| [devtools](devtools.md) | `deprecate`, `mock_data`, `compose`, `wrap_methods`, `no_debug` |
| [testing](testing.md) | `fast_mode`, `fake_clock`, `FakeClock`, `disable_retries`, `disable_rate_limits`, `disable_caching`, `overrides`, `spy`, `spy_on`, `eventually`, `eventually_sync` |
| [errors](errors.md) | `PythonWrapError`, `TimeoutExpired`, `RateLimitExceeded`, `CircuitOpenError`, `BulkheadFull`, `BatchError`, `ValidationError`, `ArgumentTypeError` |
