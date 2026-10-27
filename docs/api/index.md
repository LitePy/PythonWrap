# API reference

Everything is importable from the top-level `python_wrap` package. These pages are generated from the docstrings, so they always match the code.

| Module | Contents |
| --- | --- |
| [errors](errors.md) | `PythonWrapError`, `TimeoutExpired`, `RateLimitExceeded`, `CircuitOpenError`, `ValidationError`, `ArgumentTypeError`, `BulkheadFull` |
| [retry](retry.md) | `retry`, `fallback`, `suppress`, `retry_on_exception` |
| [circuit](circuit.md) | `CircuitBreaker`, `circuit_breaker`, `CircuitState`, `CircuitStats` |
| [deadline](deadline.md) | `deadline`, `remaining`, `check`, `Deadline` |
| [caching](caching.md) | `memoize`, `cache`, `once` |
| [concurrency](concurrency.md) | `timeout`, `Timeout`, `rate_limit`, `RateLimiter`, `throttle`, `debounce`, `singleflight`, `run_in_thread` |
| [aio](aio.md) | `gather_limit`, `map_async`, `amap_iter`, `race`, `RaceFailed`, `run_sync` |
| [bulkhead](bulkhead.md) | `Bulkhead`, `limit_concurrency`, `Limited` |
| [timing](timing.md) | `timing`, `benchmark`, `benchmark_async`, `measure_memory`, `profile` |
| [logs](logs.md) | `log_args`, `log_return`, `audit`, `trace`, `DEFAULT_REDACT` |
| [validation](validation.md) | `validate_args`, `check_type`, `enforce_types` |
| [state](state.md) | `revert_on_failure`, `Transaction`, `transactional` |
| [devtools](devtools.md) | `deprecate`, `mock_data`, `compose`, `no_debug` |
