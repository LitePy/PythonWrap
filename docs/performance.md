# Performance

What does a decorator cost on every call? For most real code the answer is "nothing you
will notice": a network call, a database query or a disk read costs microseconds to
milliseconds, and the decorators below add well under a few microseconds each. They matter
only when you wrap something that itself takes a microsecond or less, such as a pure
function in a tight loop. This page shows the numbers so you can judge for yourself.

## Method

`benchmarks/run.py` (standard library only) wraps a trivial function, `add(a, b)`, with
each decorator and calls it in a loop. Sync decorators run in a plain `for` loop. Async
decorators run `await f(1, 2)` N times inside one event loop. The reported figure is
the best of several repeats, in nanoseconds per call, loop overhead included. The bare
function goes through the identical loop, so the `x bare` columns are slowdown factors.

Each decorator is measured on its cheap, steady-state path:

* `timing`, `log_args`, `audit`, `retry` with a logger whose level is disabled (nothing is
  emitted) and a call that succeeds first time.
* `memoize`, `cache`, `once` on a cache hit.
* `rate_limit` far below its limit, `circuit_breaker` closed, `throttle` inside its window.
* `deprecate` with the warning filtered out.

Absolute numbers depend on the machine. Compare rows with each other, not with your
laptop.

## Results

Measured on an Apple M2 Pro (10 cores, 16 GB), macOS, CPython 3.14.7, full run
(`python benchmarks/run.py`, a `--quick` run agrees within about 5 percent).

| Decorator | sync ns/call | async ns/call | sync x bare | async x bare |
| --- | ---: | ---: | ---: | ---: |
| bare call | 19 | 54 | 1.0x | 1.0x |
| timing (logging off) | 710 | 790 | 36.6x | 14.6x |
| retry (success path) | 120 | 200 | 6.2x | 3.7x |
| memoize (hit) | 1,967 | 2,062 | 101.3x | 38.1x |
| memoize(maxsize=128) (hit) | 1,974 | 2,097 | 101.7x | 38.7x |
| functools.lru_cache(128) (hit) | 41 | - | 2.1x | - |
| functools.cache (hit) | 40 | - | 2.1x | - |
| once (hit) | 58 | 160 | 3.0x | 2.9x |
| cache(60) (hit) | 2,029 | 2,174 | 104.5x | 40.1x |
| rate_limit (under limit) | 397 | 483 | 20.5x | 8.9x |
| throttle (skipped) | 209 | 250 | 10.8x | 4.6x |
| singleflight (uncontended) | 1,378 | 43,072 | 71.0x | 795.1x |
| circuit_breaker (closed) | 1,038 | 453 | 53.5x | 8.4x |
| timeout (thread fallback) | 32,812 | - | 1689.9x | - |
| timeout (signal) | 3,193 | - | 164.5x | - |
| timeout (async) | - | 1,365 | - | 25.2x |
| log_args (level off) | 176 | 257 | 9.1x | 4.8x |
| audit (logging off) | 7,989 | 7,906 | 411.5x | 146.0x |
| validate_args | 1,544 | 1,611 | 79.5x | 29.7x |
| check_type | 1,360 | 1,456 | 70.0x | 26.9x |
| enforce_types | 3,488 | 3,529 | 179.6x | 65.2x |
| deprecate (warning ignored) | 368 | 449 | 19.0x | 8.3x |
| mock_data (disabled) | 92 | 172 | 4.7x | 3.2x |
| mock_data (enabled) | 82 | 126 | 4.2x | 2.3x |
| limit_concurrency(8) | 760 | 1,418 | 39.2x | 26.2x |
| Bulkhead(8) | 761 | 1,413 | 39.2x | 26.1x |
| batch_calls (max_size=1) | - | 49,664 | - | 916.8x |
| retry+timeout+circuit_breaker | 30,617 | 1,988 | 1576.9x | 36.7x |

The "x bare" factors look dramatic because the bare function is nearly free. Read the
nanosecond columns: 2,000 ns is two microseconds, a thousandth of a typical 2 ms
HTTP request.

## Interpretation

**Cheap (under about 250 ns).** `retry` on the success path, `once`, `throttle`,
`log_args` with its level disabled, `mock_data`. These do a few attribute reads and a
single lock or flag check. `log_args` asks the logger whether its level is enabled and skips
the argument formatting when it is not.

**Moderate (250 ns to 1.5 microseconds).** `rate_limit`, `deprecate`, `circuit_breaker`,
`limit_concurrency` / `Bulkhead`, `timing`, async `timeout`. The cost is a lock
acquire/release plus bookkeeping (a deque of timestamps, a state machine, a semaphore) and
the monotonic clock read. Async variants of lock-based decorators are not always cheaper:
`limit_concurrency` pays for an `asyncio` semaphore round trip.

**Signature binding (1.3 to 3.5 microseconds).** `memoize` / `cache`, `validate_args`,
`check_type` and `enforce_types` need to know which argument is which, so that
`f(1)`, `f(a=1)` and `f(1, b=<default>)` agree. They call `inspect.Signature.bind`
(roughly 0.8 microseconds) and `apply_defaults` on every call. That is why a `memoize` hit
costs about 2 microseconds while `functools.lru_cache`, which hashes the raw `args` and
`kwargs`, costs 40 ns. `enforce_types` adds a recursive `isinstance` walk per argument and
the return value on top.

**Expensive.**

* `timeout` on a sync function without signals starts a daemon worker thread for every
  call, about 30 microseconds. The signal path (Unix, main thread) is about 3 microseconds,
  from several `signal` and `setitimer` system calls. Use `timeout` around work measured
  in milliseconds, not in a hot loop. A stack like `retry + timeout + circuit_breaker` on
  sync code inherits the thread cost.
* `audit` builds an `AuditRecord` for every call (timestamp, caller location from a frame
  walk, bound and redacted arguments) even when the log line is filtered out, about 8
  microseconds. Use it on boundaries (admin actions, API handlers), not helpers.
* `singleflight` and `batch_calls` on `async def` create tasks and futures so that callers
  can share one execution; even the uncontended case needs a few event-loop iterations, 40
  to 50 microseconds. They exist to save far more than that by avoiding duplicate work.

## Tips

* **Do not worry about decorators on I/O-bound code.** Optimise only if a profile shows
  the wrapper in the hot path.
* **Disable what you do not read.** `log_args` skips its formatting when the logger's level
  is disabled, so raising the package logger above its `level` makes it nearly free:
  `logging.getLogger("python_wrap").setLevel(logging.WARNING)`. `timing` still reads the
  clock twice and builds its record (about 0.7 microseconds) either way.
* **Prefer `maxsize`.** Bounded `memoize(maxsize=...)` has the same hit cost as unbounded
  and keeps memory flat.
* **Keep cache keys simple.** Hashable scalars freeze fastest; lists, dicts and sets
  arguments are converted to hashable form on every call. For a hot function with expensive
  arguments, pass a `key=` function.
* **Call style does not matter.** `f(1, 2)`, `f(a=1, b=2)` and mixed calls bind at the same
  cost.
* **Place cheap, selective decorators on the outside.** A cache hit should short-circuit
  before `retry`, `timeout` or logging decorators run: put `memoize` outermost.
* **Pure CPU functions in tight loops:** `functools.lru_cache` is faster than `memoize`
  (about 40 ns against 2 microseconds). `memoize` buys argument normalisation, TTL,
  `typed`, async support and thread-safe stats.
* **Test suites:** `python_wrap.testing.fast_mode()` removes sleeps; it does not change
  the per-call costs above.

## Reproduce

From a checkout:

```bash
python benchmarks/run.py              # full run, about 15 seconds
python benchmarks/run.py --quick      # at most 5 seconds, noisier
python benchmarks/run.py --filter memoize
python benchmarks/run.py --json > bench.json
```

Run it on an idle machine, with the power-saving mode off. For a stable comparison run it
twice and compare the better numbers. Which hot spots are worth optimising is
tracked in `benchmarks/FINDINGS.md` in the repository.
