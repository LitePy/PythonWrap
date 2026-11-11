# Nightly ETL pipeline

A small batch job: pull rows from two upstream systems, convert amounts with reference data, load them into a warehouse. Everything is faked (no network, no database) and the whole test suite runs in about a second.

```console
$ python examples/projects/etl_pipeline/run.py
$ python -m pytest examples/projects/etl_pipeline/tests
```

## Layout

| File | Role |
| --- | --- |
| `run.py` | Entry point: builds the pipeline, runs it, prints metrics and health |
| `etl_app/config.py` | `Settings` (chunk size, deadline, bulkhead width, ping interval) |
| `etl_app/sources.py` | Fake source APIs (`fetch_rows`, a rate-limited status endpoint) |
| `etl_app/reference.py` | Reference data (exchange rates) |
| `etl_app/warehouse.py` | The load target and its replay-safe writer |
| `etl_app/pipeline.py` | Orchestration: one worker per source |
| `etl_app/observability.py` | Prometheus exporter and health summary |
| `tests/test_etl_pipeline.py` | Happy path, deadline and resume, chaos, bulkhead |

## Which feature does what, and why

| Feature | Where | Why it is there |
| --- | --- | --- |
| `process_batches` + `MemoryCheckpoint` | `pipeline.py` | One bad row does not stop the run, and a restart continues at the last chunk boundary instead of at row 0. In production use `RedisCheckpoint` or your own store with the same `load`/`save` |
| `Bulkhead` per source | `pipeline.py` | A saturated source cannot take the other one's slots. `max_waiting=0` fails fast with `BulkheadFull` instead of queueing |
| `timeout(..., use_signals=False)` + `checkpoint()` | `pipeline.py` | The deadline is enforced from a worker thread, which cannot kill the work. `checkpoint()` between rows makes the work stop itself, and the saved checkpoint keeps what finished. The call sits in the row *iterator*, not the row handler, because `process_batches` records handler exceptions as failed rows and would carry on |
| `idempotent(key=...)` | `warehouse.py` | A chunk replayed after a crash writes nothing twice: the key is the business id |
| `throttle(interval, key=...)` | `pipeline.py` | The source's status endpoint allows a ping a minute. `throttle` skips extra calls (it does not wait), with its own window per source via `key=` |
| `retry` | `pipeline.py` | Connection errors on a fetch are retried; other errors are not |
| `memoize(ttl=...)` | `reference.py` | Exchange rates are fetched once for both sources |
| `PrometheusExporter` | `observability.py` | Counters for retries, timeouts, rejections and one `etl_health_up` gauge per registered component |
| `HealthRegistry.summary(critical=...)` | `observability.py` | A failed source marks the service `degraded`, only the warehouse going away makes it `down` |
| `chaos.inject` + `fault_point` | `sources.py`, tests | The tests make the fetch fail twice (recovered) and for good (reported, health degrades) |

## What the tests prove

* Happy path: 16 rows loaded, rates fetched once, one progress ping per source despite several chunks, health `ok`, metrics rendered.
* Failure: a source that is too slow hits its deadline, stops at the next `checkpoint()`, and the next run loads only the rest. The warehouse saw each row written exactly once.
* Chaos: two injected connection errors are absorbed by the retry; a permanent outage is reported per source and turns health `degraded`.
* Bulkhead: with the CRM slots taken, CRM fails fast and billing still loads.
