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
