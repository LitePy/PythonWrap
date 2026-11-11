# Orders service

A small e-commerce style orders service, split into modules the way a real app is, with python-wrap supplying the resilience. Everything outside the process (payment gateway, product database, mail relay) is an in-memory fake in `orders/fakes.py`, so it runs offline and finishes in well under a second. The names are invented.

## Run it

```console
$ pip install python-wrap
$ python main.py          # prints what happened at each step
$ python -m pytest tests  # 12 tests: happy path, failures, replay, health, doctor
```

## Layout

| File | Role |
| --- | --- |
| `main.py` | End-to-end demo: builds the app, runs eight scenarios, prints the metrics |
| `orders/config.py` | `Settings` dataclass, optionally read from `ORDERS_*` environment variables |
| `orders/policies.py` | The payment gateway policy, built from the `http_client` preset |
| `orders/payments.py` | Gateway client: one function wrapped in that policy, plus domain errors |
| `orders/inventory.py` | Stock and price lookups with `memoize` + `singleflight` |
| `orders/service.py` | `create_order`: idempotent, rate limited, counted |
| `orders/outbox.py` | Confirmation emails through a journaled `buffered` writer |
| `orders/telemetry.py` | An event listener that counts python-wrap events |
| `orders/app.py` | Wiring, including the `HealthRegistry` |
| `orders/fakes.py` | The fake gateway, database and mailer |

## python-wrap features used, and why

* **`presets.http_client` policy** (`policies.py`): a timeout, retry with backoff and a circuit breaker for the gateway, in the right order, in one line. `idempotent=True` is only correct because every charge carries an idempotency key; `doctor` checks the policy and, in a test, shows it would reject the same retries for a non-idempotent call.
* **`memoize(ttl=)` over `singleflight`** (`inventory.py`): stock lookups are cached for 30 seconds, and 20 concurrent cache misses become one database query. The cache is cleared when stock is reserved, so it never serves a count you just changed.
* **`idempotent`** (`service.py`): the key is `user:request_id`, so a client that retries a timed-out request gets the stored order back instead of a second charge. Failures are not stored, so the retry after an outage works.
* **`rate_limit(key=user)`** (`service.py`): each user gets their own allowance. It sits inside `idempotent`, so replaying an accepted request costs nothing.
* **`buffered(journal=SQLiteJournal)`** (`outbox.py`): orders return as soon as the email is journaled. If the mail relay is down or the process dies, `replay()` after a restart delivers what is left (at least once, so the mailer de-duplicates by id).
* **`HealthRegistry`** (`app.py`): one `summary()` with a critical set, so a down optional dependency reports `degraded` (HTTP 200) and a down gateway reports `down` (503).
* **Events and `count_calls`** (`telemetry.py`, `service.py`): retries, cache hits and rejections are counted from the event stream; `create_order.stats` has call and error counts.
* **`doctor` / `explain`** (`tests/test_orders.py`): the policy is linted in the test suite, so a later edit that makes it unsafe fails CI.

## Copying it

The pieces that matter are the factories (`payments_policy`, `Inventory`, `build_create_order`, `make_outbox`). They build their own state, so two apps in one process (or two tests) never share a cache, breaker or rate-limit window. Replace `orders/fakes.py` with real clients and keep the rest.
