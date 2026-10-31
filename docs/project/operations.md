# Operations runbook and troubleshooting

How to watch python-wrap in production, what to do when something looks stuck, and how
stored state behaves across upgrades.

## What to monitor

Export events with the [Prometheus exporter](../features/observability/exporters.md) (or
StatsD, or OpenTelemetry) and alert on:

| Signal | Metric (Prometheus exporter) | Suggested alert |
| --- | --- | --- |
| A breaker is open | `python_wrap_circuit_state == 2` | open for more than 2 x `recovery_timeout` |
| Retries are burning budget | `rate(python_wrap_retry_attempts_total[5m])` vs calls | above 20 % of traffic for 10 minutes |
| Calls give up | `rate(python_wrap_retry_gave_up_total[5m])` | any sustained non-zero rate on a critical dependency |
| Load is being shed | `python_wrap_rejections_total{kind="bulkhead"}` | sustained non-zero |
| Timeouts | `python_wrap_timeouts_total` | rate above normal baseline |
| Component health | `python_wrap_health_up == 0` | any critical component |

The sample [Grafana dashboard](https://github.com/LitePy/PythonWrap/blob/main/docs/assets/grafana-dashboard.json)
plots all of these. For chat notifications on breaker opens use
[`WebhookAlerter`](../features/observability/alerts.md).

## Troubleshooting

### A circuit breaker stays open

1. Check the dependency itself first: an open breaker is usually telling the truth.
2. Inspect it: `breaker.stats` (state, failures, rejected) or `health.report()`.
3. Every half-open trial fails? The probe call may be failing for a reason unrelated to
   the outage (bad credentials after a rotation, a changed endpoint). Look at the
   exception type of the last failures (`attempt_history`, Sentry breadcrumbs).
4. Shared breakers (`RedisBreakerState`, `SQLiteBreakerState`): all processes share one
   state, so a single noisy worker can hold it open for everyone. Check that every
   process uses the same `name` **and the same settings**; different `failure_threshold`
   values against one key behave unpredictably.
5. To recover manually after fixing the dependency: `breaker.reset()` (shared state is
   reset for every process). To take a dependency out deliberately: `breaker.force_open()`.
6. Excluded exceptions (`exclude=`) and exceptions outside `on=` never count; a 4xx
   wrapped as a generic `Exception` will count unless you exclude it.

### A lease or lock seems stuck

- `AlreadyRunning` raised for a job that is not running: the previous holder crashed and
  the lease has not expired yet. Redis leases expire after `ttl` (auto-renewed while
  held); wait `ttl` or delete the key (`python-wrap:lease:<key>` by default) after
  confirming no process owns it.
- `LeaseLost` inside a long task: renewal failed (Redis unreachable or the process paused
  longer than `ttl`, e.g. a GC pause or a laptop sleep). The task must stop writing:
  another worker may already hold the lease. Increase `ttl` and client I/O timeouts.
- SQL session locks (`PgAdvisoryLock`, `SqlAppLock`) last as long as their connection: a
  pooled connection that is never returned keeps the lock. Use a dedicated pool and create
  engines after fork.

### Retries make an outage worse

Attach a `RetryBudget` (shared per dependency) or set one globally with
`configure(retry={"budget": RetryBudget(...)})`. Run `python -m python_wrap doctor` on your
policies: PW008 lists retry layers without a budget, PW004 retries without timeouts.

### Rate limiter rejects too much / too little

Process-local limiters (`rate_limit`, `RateLimiter`) count per process: N workers allow N
times the rate. Use `RedisWindow` + `shared_rate_limit` for a global limit.

### The event loop stalls

A synchronous cache store (`RedisStore`) or a sync health check is doing network I/O on
the loop. Use `amemoize` with `AsyncRedisStore`, and `health.asgi_app()` (which runs checks
in a thread).

### Timeouts do not stop the work

On Windows, in non-main threads, or with `use_signals=False`, sync timeouts cannot stop
the function (see [limitations](limitations.md#timeout)). Prefer `async def` or the client
library's own timeout.

## Stored state and upgrades

Distributed backends write the following keys. Their **formats are versioned by key
prefix**: an incompatible change ships under a new prefix (old keys simply expire or are
ignored) and is listed under *Breaking changes*, so mixed-version fleets during a rolling
deploy never misread each other's state.

| Backend | Default key / table | Value | Expiry |
| --- | --- | --- | --- |
| `RedisStore` / `AsyncRedisStore` | `<your prefix><sha256>` | codec output (pickle by default) | entry TTL |
| `RedisLock`, `RedisSemaphore` | `python-wrap:lease:<key>` | owner tokens | lease TTL |
| `RedisBreakerState` | `python-wrap:breaker:{<sha256 of name>}` (+`:failures`) | hash of state fields | none (small, fixed size) |
| `RedisWindow` | your `key` | sorted set of timestamps | window period |
| `Quota` (Redis) | `python-wrap:quota:<name>` | integer | your TTL |
| `RedisCheckpoint` | your `key` | integer index | none |
| `RedisSource` switches | `python-wrap:switch:<name>` | `0`/`1` strings | none |
| `idempotent` leases | `python-wrap:idempotency-locks:<hash>:` | owner tokens | lease TTL |
| `SQLiteBreakerState` | `breakers` and `breaker_failures` tables in your file | rows | none |

Upgrade notes:

- Changing a breaker's **settings** does not require clearing state; changing its **name**
  starts a fresh state (the old key stays until deleted).
- Changing a cache's codec (pickle to JSON) requires a new store `prefix`: old entries
  would not decode. Undecodable entries are treated as misses when `fail_open=True`.
- Lease, window and quota keys need no migration; they expire on their own.

## Graceful shutdown

Flush buffered writers and stop periodic jobs on SIGTERM with
[graceful shutdown](../features/concurrency/graceful-shutdown.md) helpers, and give
Kubernetes a `terminationGracePeriodSeconds` longer than your drain timeout.

## Release readiness

| Item | Status |
| --- | --- |
| Stable core declared and pinned by tests | Done ([stability](stability.md)) |
| Threat model and serialization/redaction review | Done ([threat model](threat-model.md)); external review pending |
| Prometheus/StatsD exporters, runbook | Done |
| Live Redis, PostgreSQL and SQL Server jobs in CI | Done (`backend-integration` jobs) |
| Coverage floor enforced | Done (`fail_under` in `pyproject.toml`) |
| Production pilot with soak results | Pending: see the [pilot guide](pilot.md) |
