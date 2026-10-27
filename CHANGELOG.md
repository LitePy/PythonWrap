# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is below `1.0` the public API may change between minor versions.
Breaking changes are listed under **Breaking changes**, each with its migration.

## [Unreleased]

## [0.2.0] - 2026-10-27

A ground-up rework. Every decorator now works on plain functions, `async def` functions and
methods, keeps the wrapped signature for type checkers, and never configures logging. Several
v0.1 decorators that did not do what their names promised (`transactional`, `revert_on_failure`,
async `timing`) now work. See the
[migration guide](https://python-wrap.readthedocs.io/en/latest/migration/) for the full upgrade
path.

### Highlights

- **Zero dependencies, fully typed.** `wrapt` and `typing-extensions` are gone; signatures are
  preserved with `ParamSpec`, the package ships `py.typed`, and `mypy --strict` is clean.
- **Sync and async with one API.** `async def` wrappers stay coroutine functions and never block
  the event loop.
- **A real resilience toolkit.** `retry` with backoff, jitter and hooks; `CircuitBreaker`;
  `fallback`; `suppress`; `timeout` that actually interrupts.
- **A real concurrency toolkit.** `memoize` with TTL and LRU, `rate_limit` with a sliding window,
  `throttle`, `debounce` and `singleflight`.
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
- Exceptions in `python_wrap.errors`, each subclassing the closest builtin: `PythonWrapError`,
  `TimeoutExpired` (`TimeoutError`), `RateLimitExceeded`, `CircuitOpenError`, `ValidationError`
  (`ValueError`) and `ArgumentTypeError` (`TypeError`).
- `once` gained `.reset()` and `.called`.
- A `py.typed` marker, and a `NullHandler` on the `python_wrap` logger.

### Changed

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

### Fixed

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
  3.14, and a wheel check.
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

[Unreleased]: https://github.com/LitePy/PythonWrap/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/LitePy/PythonWrap/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LitePy/PythonWrap/releases/tag/v0.1.0
