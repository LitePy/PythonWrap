# Framework request and task context

The adapters are optional imports. Install Flask, FastAPI/Starlette, Django, or Celery in
your application as usual; python-wrap has no runtime dependency on any of them.
They bind context and an optional **cooperative** deadline. They do not cancel an entire
request or task, replace framework logging, or start periodic jobs at import.

Attach `ContextFilter` to application log handlers if you want the bound fields on all
records; see [production observability](../features/observability/production.md).

## Flask

<!-- skip -->
```python
from flask import Flask, g
from python_wrap.flask import init_app

app = Flask(__name__)
init_app(app, user=lambda: getattr(g, "user_id", "-"), budget=30,
         health_path="/health/dependencies")
```

`before_request` binds `request_id` from a valid `X-Request-ID` header or a generated id,
and the optional user resolver. `teardown_request` restores the prior context even when
the view fails. Repeated initialization on the same app is a no-op. `health_path` is
optional; omit it if the app should not expose dependency state.

## FastAPI and Starlette

<!-- skip -->
```python
from fastapi import FastAPI
from python_wrap.fastapi import install

app = FastAPI()
install(app, budget=30, health_path="/health/dependencies")
```

The pure ASGI middleware covers HTTP dispatch and streaming, and restores context after
errors or cancellation. Non-HTTP scopes pass through unchanged. `user=` may be a callable
of the ASGI scope; an awaitable result is awaited. Install before startup. A health route
is added only when requested; Starlette users can use `ContextMiddleware` directly and
mount their own authorized view.

## Django

<!-- skip -->
```python
MIDDLEWARE = [
    "python_wrap.django.ContextMiddleware",
    # ...the application's other middleware...
]
```

The middleware supports sync and async dispatch. It binds a request id and, when present,
the authenticated user's primary key. Put it after authentication middleware when the
user field is needed. A configured factory can pass `budget=` and `user=` to the middleware
constructor. Context covers view dispatch; streaming iterator code should bind its own
context. A custom health view can return `health.report()` under your application's
normal authorization rules.

## Celery

<!-- skip -->
```python
from celery import Celery
from python_wrap.celery import install

app = Celery("worker")
context_handle = install(app, use_soft_deadline=True)
# During shutdown, if uninstalling the integration:
context_handle.close()
```

Task pre/post-run signals bind `task_id` and `task_name` and restore the previous context.
With `use_soft_deadline=True`, the task's configured soft limit becomes a cooperative
budget for python-wrap retry/timeout code; Celery retains ownership of actual task time
limits. Signal handlers are scoped to this app. Installation returns a disconnect handle
and repeated installation reuses the active handle.

### Retrying tasks with Celery and python-wrap together

Keep the two retry layers apart: python-wrap's in-process `retry` handles short blips
inside one task run (seconds), and Celery's task retry re-queues the task for longer
outages (minutes), surviving worker restarts. `retry_task` gives Celery's layer the same
schedule rules, and uses the wait a dependency asked for (`CircuitOpenError.retry_after`,
`RateLimitExceeded.retry_after`, a `Retry-After` header) instead of guessing:

<!-- skip -->
```python
from python_wrap import CircuitOpenError, retry, timeout
from python_wrap.celery import retry_task, shared_breaker

vendor_breaker = shared_breaker("vendor-a", redis_client, failure_threshold=5)


@vendor_breaker
@retry(max_attempts=3, delay=0.5, on=ConnectionError)
@timeout(10)
def fetch_report(day: str) -> bytes: ...


@app.task(bind=True, max_retries=5)
def import_day(self, day: str) -> None:
    try:
        store(fetch_report(day))
    except (ConnectionError, CircuitOpenError) as exc:
        retry_task(self, exc, delays=[60, 300, 900])
```

Prefer this over `autoretry_for=` for dependency errors: `autoretry_for` retries with
its own backoff and cannot see the breaker's `retry_after`. If you do use
`autoretry_for`, leave `CircuitOpenError` out of it so an open circuit re-queues with
`retry_task`'s countdown rather than immediately.

Prefork workers each hold their own in-process breaker state, so without a shared
backend every child probes a dead dependency separately. `shared_breaker` stores the
state in Redis (or any state backend) under one stable name per dependency; see
[multi-process breakers](../features/resilience/multi-process-breakers.md).

## Worker lifecycle

Create database engines and backend clients in worker initialization when their libraries
require it. On POSIX, python-wrap resets its registered local locks and in-flight cache/
singleflight coordination in the child. A periodic loop inherited from the parent is
stopped and must be started explicitly in the child. A buffered writer discards inherited
pending records to avoid duplicate submissions. Neither adapter automatically launches
application background work after fork.

See [Flask API](../api/flask.md), [ASGI API](../api/fastapi.md),
[Django API](../api/django.md), and [Celery API](../api/celery.md).
