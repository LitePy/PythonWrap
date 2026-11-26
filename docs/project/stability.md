# API stability and deprecation policy

python-wrap is below 1.0, but it is no longer "anything may change". This page declares
which parts are **stable core**, how the rest may evolve, and how anything is retired.
`tests/test_stability.py` pins the stable core so an accidental change fails CI.

## Stable core

These names, their documented parameters and their documented behaviour are frozen. From
0.4 onward they change only in a backwards-compatible way (new keyword-only options with
defaults that keep today's behaviour), even before 1.0:

| Area | Names |
| --- | --- |
| Retry | `retry`, `retrying`, `RetryBudget`, `RetryState`, `retry_after_header`, `http_status`, `attempt_history`, `AttemptRecord` |
| Circuit breaker | `CircuitBreaker`, `CircuitState`, `circuit_breaker`, `CircuitOpenError` |
| Timeout and deadline | `timeout`, `Timeout`, `deadline`, `TimeoutExpired` |
| Policies | `policy`, `Policy`, `ROLES` (order), `Policy.with_`, `Policy.explain` |
| Presets | `presets.http_client`, `presets.db_call`, `presets.third_party_api` |
| Errors | `PythonWrapError` and every exception in `python_wrap.errors` (names and attributes) |
| Events | event names in `events.EVENT_NAMES` and their documented fields |

What "frozen" covers:

- **Signatures.** No parameter is removed, renamed or reordered; positional parameters stay
  positional. New parameters are keyword-only with a default that keeps behaviour.
- **Defaults.** A default that changes runtime behaviour (for example `max_attempts=3`, a
  preset's timeout) changes only in a **minor** release, listed under *Changed* in the
  CHANGELOG with the old value, never in a patch.
- **Exceptions.** The exception type raised for a documented situation does not change, and
  original exceptions are re-raised unchanged by `retry`.
- **Ordering.** `ROLES` keeps its order; a new role may be inserted where it belongs.
- **Events.** Event names are not renamed; fields may be added, not removed.

Not covered: `repr()` text, log message wording, private names (leading underscore),
undocumented attributes, and exact timing beyond what is documented.

## Provisional APIs

Everything else, in particular the newer integrations (`python_wrap.http`, `web`,
`exporters`, `dashboard`, `async_cache`, `chaos`, `migrate`, `celery`, distributed
backends), is **provisional**: it may change in a minor release, but every change is listed
under **Breaking changes** in the CHANGELOG with a migration note. A provisional API
graduates to stable core by being added to the table above (and to the stability test).

### Graduation

A provisional module graduates when all three hold:

1. **Soak.** A multi-process soak (`benchmarks/soak.py --processes N --redis-url ...`)
   ran for at least an hour against the real backend with no leak, no stuck lease and
   breaker and limiter totals that add up across processes.
2. **Live CI.** Its backends run in CI against the real service (Redis, PostgreSQL,
   SQL Server through pymssql and pyodbc), not only against fakes.
3. **One quiet minor release.** It went through one minor release with no breaking change.

| Module | Target | Status |
| --- | --- | --- |
| `shared`, `coordination`, `quotas`, `stores` | 0.5.0 | Live CI in place; storage formats pinned by `tests/test_stability.py`; 1-minute 4-process soak passed, 1-hour soak pending |
| `http` | 0.5.0 | CI against the real requests and httpx packages; soak pending |
| `celery` | 0.5.0 | CI against the real Celery package (no broker yet); soak pending |
| `async_cache`, `web`, `exporters`, `observability` | 0.6.0 | Changed in 0.4.0b2 |

The storage formats of the backends above are already frozen: a change to them fails the
stability test and needs a migration note, even before the module graduates.

## Support window

After a new minor release, the previous minor release gets security fixes for six months.
Bug fixes go to the current minor release only.

## Deprecation policy

1. A deprecated API keeps working for **at least one minor release** (and at least three
   months) after the release that deprecates it.
2. It emits `DeprecationWarning` (via `python_wrap.deprecate`) naming the replacement and the
   removal version, and the CHANGELOG lists it under *Deprecated*.
3. Removal happens only in a minor (pre-1.0) or major (post-1.0) release, under
   *Breaking changes* with the migration.
4. Stable-core APIs are not removed before 2.0 at all.

## Versioning

- Patch releases (`0.4.x`): bug fixes and documentation only.
- Minor releases (`0.x.0`): new features; provisional APIs may change as described.
- 1.0 will be cut once the stable core has gone one minor release without changes and the
  open items on the [production checklist](operations.md#release-readiness) are closed.

## Storage formats

Keys and values written to Redis or SQL by distributed backends are part of the
compatibility promise for rolling deploys: see
[Operations: stored state](operations.md#stored-state-and-upgrades).
