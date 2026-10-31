# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is below `1.0` the public API may change between minor versions.
Breaking changes are listed under **Breaking changes**, each with its migration.

## [Unreleased]

### Added

- Ready-made transient database error predicates: `postgres_transient`, `mysql_transient`,
  `sqlserver_transient` (pyodbc SQLSTATEs and native numbers such as deadlock 1205 and
  Azure SQL 40613), their union `db_transient` (also SQLAlchemy `connection_invalidated`)
  and `error_code(...)` for driver error numbers. No driver is imported.
- `python_wrap.http`: method-safe HTTP retries. `HttpRetryPolicy` retries idempotent
  methods (or any request carrying an `Idempotency-Key`) on transport errors and 408/425/
  429/5xx, honouring `Retry-After`, deadlines and `RetryBudget`. `RetryTransport` and
  `AsyncRetryTransport` plug into httpx, `mount_retries` into a `requests.Session`, and
  `http_retry` wraps aiohttp-style or hand-written clients. No HTTP library is imported.
- `python_wrap.web`: `IdempotencyMiddleware` (ASGI) and `WsgiIdempotencyMiddleware` dedupe
  requests carrying an `Idempotency-Key`, replaying stored responses, answering 409 for a
  concurrent duplicate and 422 for a reused key with a different body.
- `ratelimit_headers`, `too_many_requests` and `problem_response` build IETF `RateLimit-*`
  headers and RFC 9457 problem+json responses for rate-limit, open-circuit and bulkhead
  rejections, with optional FastAPI, Flask and Django glue.

## [0.3.0] - 2026-10-29

Production integration helpers, optional distributed backends, and fixes from the
application fit review. The package keeps zero runtime dependencies; applications
supply Redis clients, SQLAlchemy engines, frameworks and telemetry providers.

### Added

- Application-fit follow-up: explicit `retry`/`retrying(delays=[...])` schedules,
  repeating the final wait, and `log_values=False` for retry diagnostics that omit
  exception/result contents and tracebacks, including failing hooks/hints.
- Opt-in `memoize(copy_result=True)` isolates mutable cached results on write and
  delivery, including concurrent callers and explicit cache reads.
- Existing FastAPI application adoption guide covering tenant keys, response adapters,
  job delay contracts, and durable outbox/cache boundaries.

- `ContextFilter`, `bind` and `unbind` for context on application log records, plus optional
  Flask, FastAPI/ASGI, Django and Celery lifecycle adapters.
- Deep `redact` and `is_sensitive` helpers, opt-in `deep=True` argument redaction;
  result-based `Retry-After` and `http_status`;
  composable `Predicate`, `sqlstate`, `message_contains` and `in_cause_chain` helpers.
- `RedisStore`, early-refresh `expiring` credential loaders, named `PolicyRegistry`,
  global `policies`, and call-time `resilient` policy resolution.
- Owner-checked local/Redis leases, `single_instance`, PostgreSQL advisory and SQL Server
  application-lock adapters, and retained-result `idempotent` execution.
- Redis sliding windows, run `Quota` budgets, shared breaker state, failure windows,
  and lease-backed shared bulkhead capacity. `CircuitBreaker.force_open()` supports
  maintenance and tests.
- Bounded sync/async `buffered` writers, per-item batch processing outcomes and explicit
  memory/Redis checkpoints, and dynamic environment/Redis switches.
- Opt-in OpenTelemetry event spans and bounded-label metrics, `timed` slow-call detection,
  an explicit weak health registry, `disabled`, `assert_retried` and `assert_cache_hit`.
- Feature guides, public API pages, worker-lifecycle recipes and a 0.3 migration guide.

### Fixed

- Redaction no longer masks harmless author/activity-session names by substring.
  Deep redaction masks cycles and uninspected containers at its depth limit.
- Retry hints can inspect rejected return values; a failing ordinary `on_retry` hook
  is logged instead of replacing the protected function's outcome.
- Registered local locks and in-flight coordination are rebuilt after POSIX fork;
  inherited periodic loops require explicit child startup.
- External cache deadlines use the live wall clock. The unsafe handwritten Redis
  recipe is replaced by a namespaced adapter that does not scan for statistics.

### Behavior changes

- Default log argument redaction matches whole name segments. Use
  `redact_match="substring"` for the old mode, and explicitly list actual credentials
  named `session_id` in applications where that name is sensitive.
- `CacheInfo.currsize` may be None for an external store with an unknown size.
- `CircuitOpenError` is never retried. Ordinary `on_retry` failures are swallowed after
  logging; hook cancellation and process-exit exceptions still propagate.
- Default coordination remains in-process. Shared backends are explicit, clients need
  finite timeouts, and leases/idempotency cannot guarantee exactly-once effects across
  crashes or lease expiry. See the migration and limitation guides.

## [0.2.1] - 2026-10-29

Publishes the completed 0.2 release under a fresh version, preserving the older
`v0.2.0` tag from the failed publishing attempt.

A ground-up rework and a much larger toolkit. Every decorator works on plain functions,
`async def` functions and methods, keeps the wrapped signature for type checkers, and never
configures logging. Several
v0.1 decorators that did not do what their names promised (`transactional`, `revert_on_failure`,
async `timing`) now work. See the
[migration guide](https://pythonwrap.readthedocs.io/en/latest/project/migration/) for the full upgrade
path.

### Highlights

- **Zero dependencies, fully typed.** `wrapt` and `typing-extensions` are gone; signatures are
  preserved with `ParamSpec`, the package ships `py.typed`, and `mypy --strict` is clean.
- **Sync and async with one API.** `async def` wrappers stay coroutine functions and never block
  the event loop.
- **A real resilience toolkit.** `retry` with backoff, jitter and hooks; `CircuitBreaker`;
  `fallback`; `suppress`; `timeout` that actually interrupts.
- **A real concurrency toolkit.** `memoize` with TTL and LRU, `rate_limit` with a sliding window,
  a shared `RateLimiter`, `Bulkhead`, `throttle`, `debounce` and `singleflight`.
- **Retry that honours the server.** `retry(delay_from=retry_after_header)` follows `Retry-After`,
  `max_time=` and `deadline()` cap the total time, and cancellation is never retried.
- **An async toolkit.** `gather_limit`, `map_async`, `amap_iter`, `race` and `run_sync`, plus
  `batch_calls` (the DataLoader pattern) and `hedge`.
- **Policies and configuration.** `policy(...)` applies protections in the recommended order, and
  `configure()` sets project-wide defaults.
- **Observable and testable.** `count_calls`, one `python_wrap.events` hook for every component,
  `log_context`, and `python_wrap.testing` (`fast_mode`, `FakeClock`, `spy`, pytest fixtures).
- **Fast.** The `memoize` hit path is about 9x faster and the validation and logging decorators
  skip signature binding on plain calls.
- **Safe by default.** Importing the package no longer touches the root logger, and `log_args`,
  `audit` and `trace` redact secrets.
- **Works on Windows**, and is tested on Python 3.10 to 3.14 on Linux, macOS and Windows.

### Breaking changes

- **`run_in_thread` returns a `concurrent.futures.Future`** instead of a `Thread`. Migrate:
  replace `.join()` with `.result()` (which also returns the value and re-raises errors).
- **`benchmark` is a plain function**, `benchmark(func, *, args=(), kwargs=None, runs=5, warmup=1)
  -> BenchmarkResult`, instead of a decorator that silently ran the function three times per
  call. Migrate: call `benchmark(func, args=(...))`; use `benchmark_async` for coroutines.
- **`retry_on_exception` is deprecated and now stops after 3 attempts** by default (v0.1 retried
  forever with no delay). Migrate: use `retry(on=...)`, and pass `max_attempts=None` to keep the
  old unlimited behaviour.
- **`rate_limit` raises `RateLimitExceeded`** instead of a bare `Exception`. Migrate: catch
  `RateLimitExceeded` (use `exc.retry_after`), or pass `block=True` to wait instead.
- **`log_args`, `log_return` and `trace` log at `DEBUG` by default** (`log_args` used `INFO`).
  Migrate: pass `level=logging.INFO` to keep the old level.
- **`no_debug` is a deprecated no-op** and will be removed in 0.3. Migrate: delete the decorator.
- **`validate_args` and `check_type` raise `TypeError` at decoration time** for a name that is not
  a parameter of the function. Migrate: fix the misspelled or stale parameter name.
- **Python 3.8 and 3.9 are no longer supported.** Migrate: upgrade to Python 3.10 or newer, or
  stay on 0.1.0.
- **Importing the package no longer calls `logging.basicConfig`.** Migrate: configure logging in
  your application if you relied on python-wrap's root-logger setup.

### Added

Resilience:

- `RetryBudget` (with `RetryBudgetStats`): one Finagle/Envoy-style retry allowance shared
  across functions, threads and event loops via `retry(budget=...)`, `retrying(budget=...)` or
  `configure(retry={"budget": ...})`. When it is spent, retrying stops at once and emits
  `retry.budget_exhausted`, which prevents retry storms against a failing dependency.
- `retry_if_result=` on `retry`/`retrying` retries when the function returns a matching value;
  once retries run out the last value is returned. `retrying` blocks set `attempt.result`.
- For `async def` functions, `retry_if`, `retry_if_result` and `on_retry` may be async and are
  awaited; `async with attempt:` is supported in `retrying`.
- `timeout(..., cleanup_grace=)` bounds async cleanup after the limit: the call is cancelled,
  given the grace to finish its cleanup, then `TimeoutExpired` is raised.
- `retry`: `delay_from=` (with the `retry_after_header` helper for `Retry-After`), `max_time=`,
  `log_level=` and `log_exc_info=`; `retry_stats(func)` and `RetryStats`; `retrying(...)` /
  `Retrying` for retrying an inline block (`for` / `async for` with `with attempt`); retries
  honour `deadline()`. Cancellation, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are
  never retried.
- `CircuitBreaker`: `success_threshold`, `exclude=`, `on_state_change` and `.stats`
  (`CircuitStats`).
- `timeout` is also a context manager (`with timeout(5):` / `async with timeout(5):`) and returns
  a `Timeout` object. `python_wrap.deadline` (`deadline()`, `remaining()`, `check()`) gives code a
  shared time budget, and `timeout` clamps to it.
- `hedge(delay, max_extra=, on=)`: hedged requests for idempotent calls; the first success wins
  and async losers are cancelled.

Async toolkit and concurrency:

- `periodic(interval, *, jitter=, initial_delay=, max_runs=, on_error=)` runs a sync function in
  a daemon thread, or an async function as a task, every N seconds without drift (missed ticks
  are skipped). The function stays callable and gains `start()`, `stop()`, `running`, `runs`,
  `last_error` and `running_in_background()` for clean shutdown. Events `periodic.run` and
  `periodic.failed`.
- `python_wrap.aio`: `gather_limit`, `map_async` and `amap_iter` (bounded concurrency, lazy
  input, ordered results; everything is cancelled and awaited on error, cancellation or early
  exit), `race` (first success wins; `RaceFailed`) and `run_sync` (refuses to run inside a running
  loop).
- `Bulkhead(max_concurrent, max_waiting=, timeout=, name=)` for sync and async (decorator, `with`,
  `async with`, `.active`, `.waiting`), `limit_concurrency(n)` and `BulkheadFull`.
- `RateLimiter`: one budget shared across functions (`with`, `async with`, `acquire`,
  `try_acquire`), and several windows at once with `rate_limit(limits=[(5, 1), (600, 900)])`.
- `batch_calls(max_size=, max_wait=, key=)`: the DataLoader pattern for sync and async code, with
  de-duplication, `.flush()` / `.aflush()` / `.pending`, and `BatchError` on contract violations.

Caching:

- `memoize(ttl=..., stale_ttl=...)`: stale-while-revalidate. After `ttl` a stale value is served
  at once while one background refresh runs (a task for async functions, a daemon thread for
  sync ones); a failed refresh keeps the stale value and is logged. Events `cache.stale` and
  `cache.refresh_failed`.
- `memoize(store=...)` and the `CacheStore` protocol: keep entries in any store (a dict, Redis,
  ...); the `cache_*` helpers work against it.
- `memoize(per_instance=True)`: per-instance method caches held through weak references, so
  instances stay collectable and need not be hashable.
- `cached_property(ttl=)` (thread-safe, `async def` getters, `invalidate()`).
- `memoize`: `cache_exceptions=` / `exception_ttl=`, a per-item `ttl=` callable, and
  `cache_contains()`, `cache_get()` and `cache_parameters()`.

Observability, configuration and testing:

- `trace(otel=True, span_name=...)` wraps calls in an OpenTelemetry span (sync and async) with
  `code.function`, `code.namespace` and `python_wrap.context.*` attributes and exception
  recording. `opentelemetry-api` is imported lazily and only when asked for; python-wrap still
  has no runtime dependencies.
- `count_calls`: calls, errors, in-flight, mean, p50, p95 and p99 per function.
- `python_wrap.events`: `subscribe()`, `listening()`, `filter_events()` and `EVENT_NAMES`, one hook
  for retry, cache, circuit, limiter, bulkhead and timeout events (Prometheus, OpenTelemetry and
  statsd recipes in the docs).
- `log_context()` / `current_log_context()`: contextvar fields in `log_args`, `log_return`,
  `audit`, `trace` and `timing` messages, on `LogRecord.context` and on `AuditRecord.context`.
  `log_args` and `audit` accept `redact_extra=`.
- `policy(...)` / `Policy`: reusable, immutable decorator bundles placed by role (`observe`,
  `validate`, `fallback`, `cache`, `limiter`, `bulkhead`, `breaker`, `retry`, `timeout`, `guard`,
  `outer`, `inner`) in the recommended order, with `.with_()`; `wrap_methods(*decorators,
  include=, exclude=, include_private=)` decorates a class.
- `python_wrap.config`: `configure()`, `configured()`, `get_config()` and `reset_config()` for
  project-wide defaults (logger, retry options, redaction), read at decoration time; explicit
  arguments win.
- `python_wrap.testing`: `fast_mode()`, `disable_retries()`, `disable_rate_limits()`,
  `disable_caching()`, `overrides()` (context managers and decorators), `FakeClock` /
  `fake_clock()`, `spy()` / `spy_on()`, `eventually()` / `eventually_sync()`, and the pytest
  fixtures `pw_fake_clock` and `pw_fast_mode` (`pytest_plugins = ["python_wrap.testing"]`).
- Typing: stacked decorators keep the exact function type (`@retry` over `@memoize` keeps
  `.cache_info()`), and `Memoized`, `Once`, `CircuitProtected`, `Limited` and `Counted` bind
  correctly under methods and `@classmethod`. Typing regression tests run `mypy` in CI.
- A cookbook of eight recipes, seven runnable `examples/`, a performance page with
  `benchmarks/run.py`, and a thread-safety stress suite (`PW_STRESS=1`, also run on free-threaded
  3.13t and 3.14t).

Core:

- Decorated functions, including `periodic` objects and `wrap_methods` classes, pickle by
  reference, so they work with `multiprocessing` and `ProcessPoolExecutor` under the "spawn"
  start method. Cache, limiter and breaker state is per worker process.
- `retry`: `backoff`, `max_delay`, `jitter`, `on=` exception filter, `retry_if=` predicate,
  `on_retry=` hook receiving a `RetryState`, unlimited attempts (`max_attempts=None`), and bare
  `@retry` usage.
- `fallback(value)` / `fallback(handler=...)` and `suppress(*exceptions)` for failing soft.
- `CircuitBreaker` (shareable across functions), the `circuit_breaker` decorator (breaker exposed
  as `.breaker`) and `CircuitState`.
- `memoize`: `maxsize` (LRU), `ttl`, `typed`, `key=`, `cache_info()`, `cache_clear()`,
  `cache_delete()`, and in-flight de-duplication for async functions.
- `throttle(interval)`, `debounce(wait)` with `.cancel()` and `.flush()`, `singleflight(key=)`,
  and `rate_limit(block=, key=)`.
- `timing(threshold=, callback=)` with `TimingRecord`, `measure_memory(callback=)` with
  `MemoryRecord`, `profile(sort=, limit=, callback=)`, and `benchmark_async`.
- `log_args` secret redaction (`redact=`, `DEFAULT_REDACT`) and `max_length=`; `audit` now yields
  a structured `AuditRecord` with `sink=` and `include_result=`; `trace(limit=)`. Every logging
  decorator accepts `logger=` and `level=`.
- `enforce_types`: runtime checks from annotations (generics, unions, `Literal`, `Annotated`,
  `NewType` and more) for arguments and return values.
- `Transaction` with `on_commit` / `on_rollback`, `tx=` injection, `current_transaction()`, and
  sync and async context-manager support.
- `revert_on_failure(attrs=, deep=, target=)`.
- `deprecate(since=, removed_in=, alternative=, category=)`, which also works on classes;
  `mock_data(enabled=bool | callable)`; `compose(*decorators)`.
- Exceptions in `python_wrap.errors`, all subclassing `PythonWrapError`: `TimeoutExpired`
  (also a `TimeoutError`), `RateLimitExceeded`, `CircuitOpenError`, `ValidationError` (also a
  `ValueError`) and `ArgumentTypeError` (also a `TypeError`).
- `once` gained `.reset()` and `.called`.
- A `py.typed` marker, and a `NullHandler` on the `python_wrap` logger.

### Changed

- `CircuitBreaker`, `RateLimiter`, `Bulkhead`, `RetryBudget`, `Transaction`, `Policy` and
  `testing.FakeClock` raise a clear `TypeError` when pickled or copied (their state belongs to
  one process), instead of an error about a thread lock or a copy that silently shares a lock.
- `RetryState.exception` can be `None` (when a returned value was rejected by
  `retry_if_result`), and `RetryState` has a new `result` field. A sync function given an async
  retry hook raises `TypeError` when decorated.
- On free-threaded Python 3.14t new threads inherit the caller's context, so `log_context`
  fields carry over into them (`sys.flags.thread_inherit_context`).
- The project uses a `src/` layout and Hatchling, and requires Python 3.10 or newer.
- The runtime dependencies `wrapt` and `typing-extensions` were unused and have been removed.
- `retry` waits with `asyncio.sleep` for `async def` functions, and re-raises the final
  exception unchanged with its original traceback.
- `timeout` raises `TimeoutExpired`. Async functions are cancelled with `asyncio.wait_for`; sync
  functions are interrupted with `SIGALRM` on the Unix main thread. The thread fallback used
  elsewhere cannot stop the work, and is now documented.
- `memoize` and `cache` keys are bound to the function signature with defaults applied, so
  `f(1)`, `f(x=1)` and `f(1, y=<default>)` share one entry; list, dict and set arguments are
  converted to hashable keys.
- `deprecate` points its warning at the caller and sets `__deprecated__` (PEP 702).
- `from python_wrap.decorators import ...` still works through a compatibility module.
- `timeout(...)` now returns a `Timeout` object, which is both a decorator and a context manager.
  Decorator use is unchanged.
- `retry` never retries cancellation-type exceptions (`asyncio.CancelledError`,
  `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`), even if `on=` lists them.
- The `redact=` default on `log_args` and `audit` is `None`, which means "the configured value, or
  `DEFAULT_REDACT`". Pass `redact=()` to turn redaction off.
- `audit` with no `sink` and a disabled log level builds no record at all.
- `Limited.bulkhead` is a read-only property.
- Test overrides apply only to backoff waits. `fast_mode()` and `fake_clock()` no longer shorten
  limits on how long to wait: async `Bulkhead`/`limit_concurrency` timeouts, `race(timeout=)`,
  async `hedge` delays and async `batch_calls`/`debounce` windows run on real time, like their
  sync versions and `timeout`.
- Under `fast_mode()`, a blocking rate limiter (`block=True`) that would wait lets the call
  through at once and still counts it.

### Fixed

- Final releases now pass through TestPyPI and an installation check before PyPI.
  Both indexes are verified after upload before creating the GitHub release.
- Source distributions include the examples and benchmark script used by their tests.
- Release smoke checks validate metadata, public imports, `py.typed`, retry, memoization
  and async behavior in an isolated installed environment.

- `AsyncPeriodic.stop()` no longer swallows a cancellation aimed at the task awaiting it, and
  ends a loop whose run swallows the cancel; `stop()` called inside a run ends the loop as soon
  as that run returns.
- `timeout(..., cleanup_grace=...)` lets an outside cancellation propagate as `CancelledError`
  (matching `asyncio.timeout`), and waits for and retrieves the child task when the caller is
  cancelled instead of logging "Task exception was never retrieved".
- `periodic(on_error="raise")` logs an error that `stop()` never collected when `start()` is
  called again, instead of dropping it silently.
- `revert_on_failure` never restored anything because its snapshot was always empty.
- `transactional` was a no-op; it now commits and rolls back through a `Transaction`.
- `memoize` and `cache` built keys from `str(args)`, which collided and ignored keyword
  equivalence. `cache()` also shared a single store across all functions it decorated.
- `timing` measured about zero seconds for `async def` functions, and now uses `perf_counter`.
- `measure_memory` used `ru_maxrss`, which is Unix-only and reported the wrong units; it now
  uses `tracemalloc`.
- The package failed to import on Windows because it imported the Unix-only `resource` module.
- `audit` reported the wrapper's own frame as the caller.
- `once` was not thread-safe, and now retries after an exception.
- `validate_args` rebuilt the function signature on every call.
- Under `fast_mode()` or `fake_clock()`, async bulkhead and `race` timeouts expired at once and
  `hedge` launched every backup attempt immediately.
- A blocking `RateLimiter`/`rate_limit(block=True)` busy-spun at full CPU under `fast_mode()`.
- `timeout`'s thread fallback reported a call as late when it advanced the fake clock (for
  example a `retry` inside it), and a nested signal-based `timeout` did not fire under
  `fake_clock()`.
- `@transactional` on a function with a positional-only (or `*tx` / `**tx`) `tx` parameter
  crashed at call time; it now raises `TypeError` when decorating.
- `singleflight`'s default key treated `f(1, b=2)` and `f((1,), (("b", 2),))` as the same call,
  and `memoize` on a callable without an inspectable signature treated `f(1, b=2)` and
  `f(1, 2)` as the same call.

### Performance

- Async `memoize` and `cache` hits are about 30-35% faster (one lock instead of two).
- The `memoize` hit path is about 9x faster.
- `validate_args`, `check_type`, `enforce_types`, `log_args` and `audit` skip signature binding on
  plain positional calls (5x to 40x faster).
- `audit` skips building a record when nobody would receive it, and `timing` skips formatting
  when its log level is disabled.
- Numbers and a benchmark script are on the
  [performance page](https://pythonwrap.readthedocs.io/en/latest/project/performance/).

### Deprecated

- `retry_on_exception`: use `retry(on=...)`. Emits a `DeprecationWarning`.
- `no_debug`: a no-op, to be removed in 0.3.
- `cache`: soft-deprecated in the docs only (no warning yet); prefer `memoize(ttl=...)`.

### Removed

- Support for Python 3.8 and 3.9.
- Poetry as the build and dependency tool (replaced by Hatchling and uv).
- The Sphinx documentation (replaced by the new documentation site).
- The token-based PyPI publish workflow (replaced by tag-driven publishing with Trusted
  Publishing).

### Infrastructure

- CI runs ruff, `mypy --strict`, the test suite on Linux, macOS and Windows across Python 3.10 to
  3.14, the test and thread-safety stress suites on free-threaded Python 3.14t, and a wheel
  check.
- Tag-driven releases publish to PyPI through Trusted Publishing, with no stored token.
- The documentation build is checked in CI and hosted on Read the Docs.
- Added Dependabot, issue and pull request templates, `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md` and `RELEASING.md`.

### Known limitations

- The `timeout` thread fallback (Windows, non-main threads) cannot stop the running work.
- `rate_limit`, `throttle` and `debounce` share state across instances when used on methods
  (`rate_limit` accepts `key=` to split it).
- `memoize` on methods keeps instances alive; use `maxsize` or `ttl`.
- Concurrent calls to `measure_memory` share one tracker, and `profile` on an async function
  includes event-loop work.
- `transactional` injects `tx=` by keyword, so declare `tx` last or keyword-only.

## [0.1.0] - 2025-01-21

Initial release.

[Unreleased]: https://github.com/LitePy/PythonWrap/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/LitePy/PythonWrap/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/LitePy/PythonWrap/compare/v0.1.0...v0.2.1
[0.1.0]: https://github.com/LitePy/PythonWrap/releases/tag/v0.1.0
