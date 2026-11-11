# Example projects

The recipes in this cookbook are single files. The projects in [`examples/projects/`](https://github.com/LitePy/PythonWrap/tree/main/examples/projects) are the next step up: small multi-file applications laid out the way a real service is, with configuration, several modules, an entry point and their own test suites. Read one to see how the decorators fit together across files, or copy it and replace the fakes with real clients.

Every project follows the same rules:

* **Offline.** Every outside system (payment gateway, database, mail relay, model API) is an in-memory fake in the project, so nothing needs credentials, a network or a service to be running.
* **Fast.** `python main.py` and the project's tests each finish in about a second. Back-off waits run on [`fake_clock`](testing-your-code.md), so they cost nothing.
* **Tested.** Each project has a `tests/` directory with happy-path tests and failure scenarios. The repository's own test suite runs every project's tests and `main.py` in a subprocess, so they cannot rot.
* **Standard library plus python-wrap.** Nothing else to install. Names and data are invented.

```console
$ cd examples/projects/orders_service
$ python main.py
$ python -m pytest tests
```

## The projects

| Project | What it is | python-wrap features |
| --- | --- | --- |
| [`orders_service`](https://github.com/LitePy/PythonWrap/tree/main/examples/projects/orders_service) | An e-commerce style orders service with a payment gateway, stock lookups, idempotent order creation and an email outbox | `presets.http_client`, `memoize` + `singleflight`, `idempotent`, `rate_limit(key=)`, `buffered(journal=SQLiteJournal)` with `replay`, `HealthRegistry`, events, `count_calls`, `doctor` |
| [`support_agent`](https://github.com/LitePy/PythonWrap/tree/main/examples/projects/support_agent) | A customer-support AI agent: tools, an agent loop and a scripted fake model | `@tool` (`read_only`, `requires_approval`, `idempotent_key`), `ToolRegistry`, `RunGuard`, `Budget`, `presets.llm_client`, `fallback_models`, `traced_llm` / `traced_tool`, `Redactor`, `AuditTape` and `replay`, `chaos.llm`, `doctor` |

## orders_service

A request to create an order passes through these layers, each in its own module:

```text
create_order            service.py    count_calls > idempotent > rate_limit(per user)
  inventory.require     inventory.py  memoize(ttl) > singleflight > database
  charge                payments.py   http_client policy: breaker > retry > timeout > gateway
  inventory.reserve     inventory.py  decrements stock, clears the cache
  outbox.submit         outbox.py     buffered(journal=SQLiteJournal): returns once journaled
```

What to look at:

* **Where policy lives.** `policies.py` builds the gateway policy from a preset; `payments.py` applies it. Changing retry behaviour never touches business code.
* **State per app.** Caches, breakers, rate-limit windows and the idempotency store are created inside factories, so tests build a fresh app instead of resetting globals.
* **Failure scenarios.** The tests cover an outage (nothing reserved, nothing mailed, and the same request id succeeds once the gateway is back), a declined card that is not retried, and a mail relay that is down while orders still succeed and the outbox replays after a restart.
* **`doctor` in CI.** One test lints the payments policy, and another shows the same policy is rejected if the charge were not idempotent.

## support_agent

One support ticket becomes an agent run:

```text
agent run (RunGuard: max steps, repeats, token Budget)
  ask(messages)         llm.py     llm_client policy > traced_llm > fallback_models(primary, backup)
  dispatch(name, args)  tools.py   ToolRegistry > ToolPolicy(approver) > @tool > traced_tool
  tape                  observability.py   Redactor + JSONL AuditTape
```

What to look at:

* **The loop knows nothing about resilience.** `agent.py` takes `ask` and `dispatch` callables. In production they are the guarded client and the registry; in the replay test they come from the saved tape, so the test runs the real loop with no model and no side effects.
* **Three kinds of tool.** A read-only lookup, a refund that needs approval and is keyed by order id (one refund per order, ever), and an email deduplicated per run.
* **A scripted model.** `provider.py` decides its next move from the conversation, like a model would, so a primary and a backup provider can share it.
* **Failure scenarios.** The tests cover a refund the approver refuses (the agent is told and finishes), a token budget and a step limit that stop a run, an injected `429` that falls back to the backup model, and two models rate limited at once so the client waits for `Retry-After` on the fake clock.

## Writing your own

Start from the project closest to your service and keep the structure:

1. Put every external system behind a small class (or a callable) so a fake can replace it.
2. Build policies and stateful decorators inside factory functions, not at import time.
3. Test the failure paths with `fake_clock`, [`chaos`](../testing/chaos.md) and `fake_llm` rather than by mocking internals.
4. Run [`doctor`](../features/resilience/presets-and-doctor.md) on the policies in a test.
