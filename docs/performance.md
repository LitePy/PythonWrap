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
| bare call | 20 | 55 | 1.0x | 1.0x |
| timing (logging off) | 507 | 586 | 25.2x | 10.6x |
| retry (success path) | 428 | 516 | 21.3x | 9.3x |
| memoize (hit) | 221 | 452 | 11.0x | 8.2x |
| memoize(maxsize=128) (hit) | 253 | 480 | 12.6x | 8.7x |
| functools.lru_cache(128) (hit) | 42 | - | 2.1x | - |
| functools.cache (hit) | 40 | - | 2.0x | - |
| once (hit) | 66 | 147 | 3.3x | 2.7x |
| cache(60) (hit) | 331 | 498 | 16.4x | 9.0x |
| rate_limit (under limit) | 646 | 759 | 32.1x | 13.7x |
| throttle (skipped) | 259 | 302 | 12.9x | 5.5x |
| singleflight (uncontended) | 1,427 | 1,646 | 70.9x | 29.7x |
| circuit_breaker (closed) | 417 | 540 | 20.7x | 9.8x |
| timeout (thread fallback) | 8,689 | - | 432.0x | - |
| timeout (signal) | 2,588 | - | 128.7x | - |
| timeout (async) | - | 1,442 | - | 26.0x |
| log_args (level off) | 182 | 260 | 9.0x | 4.7x |
| audit (logging off) | 198 | 278 | 9.8x | 5.0x |
| validate_args | 234 | 312 | 11.6x | 5.6x |
| check_type | 207 | 291 | 10.3x | 5.3x |
| enforce_types | 308 | 393 | 15.3x | 7.1x |
| deprecate (warning ignored) | 376 | 473 | 18.7x | 8.5x |
| mock_data (disabled) | 94 | 176 | 4.7x | 3.2x |
| mock_data (enabled) | 82 | 127 | 4.1x | 2.3x |
| limit_concurrency(8) | 777 | 1,434 | 38.6x | 25.9x |
| Bulkhead(8) | 787 | 1,457 | 39.1x | 26.3x |
| batch_calls (max_size=1) | - | 2,678 | - | 48.4x |
| retry+timeout+circuit_breaker | 10,914 | 2,520 | 542.6x | 45.5x |

The "x bare" factors look dramatic because the bare function is nearly free. Read the
nanosecond columns: 2,000 ns is two microseconds, a thousandth of a typical 2 ms
HTTP request.

## Interpretation

**Cheap (under about 300 ns).** `once`, `throttle`, `log_args` with its level disabled,
`audit` with logging off, `mock_data`. These do a few attribute reads and a
single lock or flag check. `log_args` asks the logger whether its level is enabled and skips
the argument formatting when it is not. `audit` returns before it builds its record when
the logger is disabled and nothing else is listening.

**Moderate (300 ns to 1.5 microseconds).** `retry` on the success path, `rate_limit`,
`deprecate`, `circuit_breaker`, `limit_concurrency` / `Bulkhead`, `timing`, async `timeout`,
`singleflight`. The cost is a lock
acquire/release plus bookkeeping (a deque of timestamps, a state machine, a semaphore) and
the monotonic clock read. Async variants of lock-based decorators are not always cheaper:
`limit_concurrency` pays for an `asyncio` semaphore round trip.

**Signature binding (about 200 to 350 ns).** `memoize` / `cache`, `validate_args`,
`check_type` and `enforce_types` need to know which argument is which, so that
`f(1)`, `f(a=1)` and `f(1, b=<default>)` agree. They call `inspect.Signature.bind` and
`apply_defaults` on every call. That is why a `memoize` hit costs about 220 ns while
`functools.lru_cache`, which hashes the raw `args` and `kwargs`, costs 40 ns. `enforce_types`
adds a recursive `isinstance` walk per argument and the return value on top.

**Expensive.**

* `timeout` on a sync function without signals starts a daemon worker thread for every
  call, about 9 microseconds. The signal path (Unix, main thread) is about 2.6 microseconds,
  from several `signal` and `setitimer` system calls. Use `timeout` around work measured
  in milliseconds, not in a hot loop. A stack like `retry + timeout + circuit_breaker` on
  sync code inherits the thread cost (about 11 microseconds in the table).
* `audit` with logging enabled builds an `AuditRecord` for every call (timestamp, caller
  location from a frame walk, bound and redacted arguments). That costs far more than the
  about 200 ns it takes with logging off. Use it on boundaries (admin actions, API handlers),
  not helpers.
* `singleflight` and `batch_calls` on `async def` create tasks and futures so that callers
  can share one execution. The uncontended case costs about 1.6 microseconds for
  `singleflight` and 2.7 microseconds for `batch_calls` (`max_size=1`). They exist to save
  far more than that by avoiding duplicate work.

## Tips

* **Do not worry about decorators on I/O-bound code.** Optimise only if a profile shows
  the wrapper in the hot path.
* **Disable what you do not read.** `log_args` skips its formatting when the logger's level
  is disabled, so raising the package logger above its `level` makes it nearly free:
  `logging.getLogger("python_wrap").setLevel(logging.WARNING)`. `timing` still reads the
  clock twice and builds its record (about 0.5 microseconds) either way.
* **Prefer `maxsize`.** Bounded `memoize(maxsize=...)` has about the same hit cost as unbounded
  and keeps memory flat.
* **Keep cache keys simple.** Hashable scalars freeze fastest; lists, dicts and sets
  arguments are converted to hashable form on every call. For a hot function with expensive
  arguments, pass a `key=` function.
* **Call style does not matter.** `f(1, 2)`, `f(a=1, b=2)` and mixed calls bind at the same
  cost.
* **Place cheap, selective decorators on the outside.** A cache hit should short-circuit
  before `retry`, `timeout` or logging decorators run: put `memoize` outermost.
* **Pure CPU functions in tight loops:** `functools.lru_cache` is faster than `memoize`
  (about 40 ns against 220 ns). `memoize` buys argument normalisation, TTL,
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
