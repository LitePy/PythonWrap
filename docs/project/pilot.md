# Running a production pilot

python-wrap's breadth is ahead of its production track record. Before relying on it for a
critical path, run a bounded pilot behind one real dependency and record soak results.
This page is the checklist; the results belong in your own runbook.

## 1. Pick one integration

Choose a single outbound dependency where today's code has a hand-written retry loop or
no protection at all: a vendor API import job, an internal HTTP service, or a database
unit of work. Avoid, for the pilot:

- **Non-idempotent writes** (payments, emails, POSTs without an idempotency key). Wrap
  them with timeout and breaker only, never retry: `third_party_api(name, idempotent=False)`.
- Paths with a strict response contract you cannot observe (keep the existing behaviour
  and add python-wrap underneath, e.g. a timeout and a breaker that only fails faster).

## 2. Replace the hand-written code with a preset

<!-- skip -->
```python
from python_wrap.presets import third_party_api

vendor = third_party_api("vendor-a", timeout_seconds=20, attempts=4, max_concurrent=4)


@vendor
def fetch_page(cursor: str) -> dict: ...
```

Run `python -m python_wrap doctor yourapp.integrations:fetch_page` and fix every error
before deploying. Keep any outer retry that already exists at the job level (Celery task
retry, a whole-file retry) and make sure the layers do not multiply: in-process retries
should be short (seconds), job retries long (minutes); see `python_wrap.celery.retry_task`.

## 3. Make it observable before it ships

- Export metrics (`PrometheusExporter` or `StatsDExporter`), import the sample Grafana
  dashboard, and alert on breaker opens (`WebhookAlerter`) and give-ups.
- Register the breaker in the health registry; expose `health.summary()`.
- Send `sentry_breadcrumbs()` (or `annotate_span`) so failures carry their retry timeline.

## 4. Soak before and during the pilot

Before deploying, run the soak script against the policy you chose (adjust the policy in
the script to match):

<!-- skip -->
```bash
python benchmarks/soak.py --duration 600 --threads 32 --json > soak-before.json
```

It cycles a simulated dependency through healthy, degraded and down phases and checks
that retries stay bounded (`amplification`), the breaker opens and recovers, and memory
does not grow. Then run the pilot for **2-4 weeks** in production and record:

| Measure | Where | Accept when |
| --- | --- | --- |
| Error rate seen by callers | your service metrics | not worse than before |
| p95/p99 latency of the wrapped call | `count_calls`, exporter histograms | within agreed budget |
| Retries per logical call | `retry_attempts_total / calls` | below ~0.2 outside incidents |
| Breaker open episodes | `circuit_state`, alerts | each one matches a real dependency incident |
| Duplicate side effects | your data checks | none |
| Process memory and threads | container metrics | flat over the pilot |
| Overhead per call | `benchmarks/run.py` on the host type | negligible next to I/O |

Use `python_wrap.chaos` in staging to rehearse an outage: inject `ConnectionError` and
latency at the dependency's fault point and check that alerts fire and the service
degrades as designed.

## 5. Write it up

Record the before/after numbers, the incidents the breaker handled, and every surprise.
Feed bugs back as issues. Then extend to the next integration, one at a time.
