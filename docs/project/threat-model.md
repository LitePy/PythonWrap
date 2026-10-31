# Threat model and security review notes

This page records what python-wrap trusts, what it protects, and the review of its
serialization and redaction surfaces. It complements [SECURITY.md](https://github.com/LitePy/PythonWrap/blob/main/SECURITY.md)
(how to report a vulnerability). It is a maintainer self-review, not an external audit.

## Assets and trust boundaries

| Boundary | What crosses it | Trusted? |
| --- | --- | --- |
| Decorated application code | arguments, results, exceptions | Yes: python-wrap runs inside your process with your privileges |
| Redis (`RedisStore`, `AsyncRedisStore`, `RedisLock`, `RedisBreakerState`, `RedisWindow`, `RedisCheckpoint`, `RedisSource`) | cache entries, lease tokens, counters, breaker state, switch values | **Only as far as you trust everyone who can write to that Redis** |
| SQL (`PgAdvisoryLock`, `SqlAppLock`) and SQLite (`SQLiteBreakerState`) | lock resources (hashed), breaker state rows | Same as the database credentials |
| HTTP clients (`python_wrap.http`) | `Retry-After` headers, status codes | **Untrusted**: values are parsed defensively and capped |
| HTTP servers (`python_wrap.web`, `health.*_app`, `exporters`, `dashboard`) | request headers and bodies, scraped metrics | **Untrusted** input; output may disclose internals |
| Environment (`policies.load_from_env`, `EnvSource`, `chaos.enable_from_env`) | JSON configuration | Trusted: whoever sets env vars already controls the process |
| Logs, events, metrics, error trackers | function names, exception types, timings | Destinations may be widely readable |

## Serialization review

**Pickle is only ever *loaded* from caches you configured.** `RedisStore` and
`AsyncRedisStore` default to `pickle.dumps`/`pickle.loads` so arbitrary Python results
round-trip. Unpickling attacker-controlled bytes executes code, so:

- Anyone able to write keys under the store's prefix can execute code in every process
  that reads the cache. Treat write access to that Redis as equivalent to code execution.
- For shared or less-trusted Redis instances pass a non-executing codec:
  `dumps, loads = python_wrap.async_cache.json_codec()` (JSON-shaped results only), or your
  own signed/encrypted codec (for example HMAC-verify before `loads`).
- Keys are hashed (`sha256` of a pickle of the arguments, **dump only**), so a cache key
  never reveals arguments and never loads data.

Other backends never unpickle: lease, breaker, window, quota and checkpoint backends store
integers, tokens and states via Lua scripts or parameterised SQL. `python_wrap.web`
stores idempotent responses as plain dicts (status, headers, latin-1 body), never pickles.
Policies, switches and chaos rules from the environment are parsed with `json.loads` and
validated before use. In-process objects that hold closures or locks (`Policy`,
`RetryBudget`, `CircuitBreaker`) refuse to pickle (`_core.refuse_pickle`), so they cannot
be smuggled through a pickle-based queue by accident.

SQL: lock resource names are SHA-256 hashes passed as bound parameters; no SQL is built
from user input. SQLite breaker state uses parameterised statements only.

## Redaction review

`redact`, `is_sensitive` and `DEFAULT_REDACT` mask values whose **name** looks sensitive
(`password`, `token`, `secret`, `api_key`, `authorization`, `cookie`...), matching name
segments rather than substrings so `author` or `session_date` stay visible while
`session_token` and `apiKey` are masked. Review findings and the resulting guarantees:

- Matching is by name. A secret stored under an innocuous name (`data`, `value`) is not
  detected; pass `redact_names=` or keep secrets out of logged arguments.
- Deep redaction walks dicts, lists, tuples and dataclass-like mappings without mutating
  the original; cycles are bounded.
- `retry(log_values=False)`, `observability.sentry_breadcrumbs`, `attempt_history`,
  `exporters`, `dashboard`, `alerts` and `health` reduce exceptions to their **type name**:
  exception messages (which often contain URLs, SQL, or credentials) are never exported.
- Webhook URLs used by `WebhookAlerter` are redacted from its log lines.

Regression tests: `tests/test_redaction.py`, `tests/test_retry_log_privacy.py`,
`tests/test_attempt_history.py` (messages never kept), `tests/test_health_summary.py`.

## Denial-of-service surfaces

- Unbounded growth: caches (`maxsize`), exporter series (`max_series`), dashboard function
  table, per-key limiters (`max_keys`) and the idempotency middleware body buffer
  (`max_body`) are all bounded; report any structure documented as bounded that is not.
- `Retry-After` values from servers are capped (`max_retry_after`, `max_delay`, and for
  `retry(delay_from=...)` `HINT_CEILING`, 600 s, when `max_delay` is unset) so a hostile
  upstream cannot park a worker indefinitely.
- `MemoryResultStore` keeps at most `max_entries` results (default 10,000).
- Retries are bounded by `max_attempts`/`max_time` and can share a `RetryBudget`; the
  doctor flags unbounded retries (PW001).

## Idempotency middleware

Idempotency keys come from untrusted clients. The middleware:

- partitions keys by `scope=` or, by default, a digest of `Authorization` and `Cookie`,
  so one client can never replay another client's response (pass `scope=` with your
  authenticated tenant/user id for the strongest isolation);
- fingerprints method, path, query string and body, so a reused key with different input
  is rejected with 422;
- never stores `Set-Cookie` or hop-by-hop headers, and never stores 5xx responses;
- bounds request bodies with `max_body`, including chunked WSGI bodies.

Stored responses are kept as-is in the configured store: do not use the middleware on
endpoints whose responses carry secrets unless that store is trusted.

## Alerts and outbound calls

`WebhookAlerter` accepts only `http`/`https` URLs, never logs the full URL, sends no
exception messages, and escapes Slack control characters. The URL is operator
configuration: treat it as trusted, as with any webhook (it can reach internal hosts).

## Operational exposure

The `/metrics`, health and dashboard apps have **no authentication**. They disclose
function and component names, breaker states and rates. Mount them on an internal port or
behind your existing auth. `Dashboard.serve()` binds to `127.0.0.1` by default.

`python_wrap.chaos` injects failures only when rules are installed in code or the
`PW_CHAOS` variable is set; it is inert otherwise. Do not set `PW_CHAOS` in production
deployments unless you are running a deliberate game day.

## Accepted risks

- `RedisStore`/`AsyncRedisStore` default to pickle for backward compatibility. Only use them
  with a Redis instance you fully trust, or pass `json_codec()`.
- `PW_CHAOS` and policy files may name an exception as `module:Class`; resolving it imports
  that module. These are deployment configuration, never request input, and anything that
  is not an exception class is rejected.

## Not yet done

- An independent third-party security review (needs a reviewer outside the project).
- Fuzzing of the `Retry-After`/HTTP-date parser and idempotency middleware request
  handling beyond the property-based tests.
