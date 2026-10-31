# Web helpers

Server-side helpers for HTTP services: safe POST retries with an `Idempotency-Key` header, and standards-shaped responses when a rate limit, circuit breaker or bulkhead rejects a request. Nothing here imports a web framework; the middleware is plain ASGI/WSGI.

## Safe POST retries end to end

A client that times out on `POST /payments` cannot know whether the server acted. The fix is a pair:

1. The client sends the same random `Idempotency-Key` on every retry of one logical operation (see [retry](resilience/retry.md)).
2. The server middleware runs the handler once, stores the response, and replays it for later requests with that key.

```python
import asyncio
import json
from python_wrap.web import IdempotencyMiddleware

charges = []

async def app(scope, receive, send):
    msg = await receive()
    charges.append(msg["body"])
    await send({"type": "http.response.start", "status": 201,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps({"id": len(charges)}).encode()})

wrapped = IdempotencyMiddleware(app, ttl="1h")

async def post(key: str, body: bytes):
    scope = {"type": "http", "method": "POST", "path": "/payments",
             "headers": [(b"idempotency-key", key.encode())]}
    queue = [{"type": "http.request", "body": body}]
    sent = []

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    await wrapped(scope, receive, send)
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    return sent[0]["status"], headers, sent[1]["body"]

async def main():
    first = await post("order-42", b'{"amount": 10}')
    retry = await post("order-42", b'{"amount": 10}')
    assert first[0] == retry[0] == 201 and first[2] == retry[2]
    assert retry[1]["idempotent-replayed"] == "true"
    assert len(charges) == 1                       # handler ran once
    mismatch = await post("order-42", b'{"amount": 99}')
    assert mismatch[0] == 422                      # same key, different body

asyncio.run(main())
```

## Semantics

| Situation | Result |
| --- | --- |
| First request with a key | Handler runs; response stored unless its status is 500 or higher |
| Same key, same body | Stored response replayed with `Idempotent-Replayed: true` |
| Same key, different body | `422` problem+json |
| Same key while the first is still running | `409` problem+json with `Retry-After: 1` |
| Header missing and `required=True` | `400` |
| Body larger than `max_body` | `413` (never buffered unbounded) |
| No header, or a method not in `methods` | Passed straight through, streaming untouched |

The key is combined with the method, path and your `scope(...)` value, then hashed, so the same string on two endpoints does not collide. Responses with status 500 or higher are not stored, so the client can retry them. Client errors (4xx) are stored and replayed.

!!! warning "Scope keys by authenticated identity"
    Pass `scope=lambda asgi_scope: tenant_or_user_id` (for WSGI the callable receives `environ`). Without it, two users who send the same key share one stored response.

## Distributed deployments

By default the store and lease are in-process. With several workers or hosts, pass both a shared result store and a shared lease backend (for example a Redis-backed store and `RedisLock`; see [leases and idempotency](concurrency/leases-and-idempotency.md)). Stored values are plain dicts (`status`, `headers`, `body` as latin-1 text, `fp`), never pickled, so a JSON-serialising store works. Set `lease_ttl` longer than your slowest handler.

## Wiring

<!-- skip -->
```python
# FastAPI / Starlette
from fastapi import FastAPI
from python_wrap.web import IdempotencyMiddleware, fastapi_exception_handlers

app = FastAPI()
app.add_middleware(
    IdempotencyMiddleware,
    scope=lambda s: dict(s["headers"]).get(b"x-tenant", b"").decode(),
    required=True,
)
for exc_type, handler in fastapi_exception_handlers().items():
    app.add_exception_handler(exc_type, handler)
```

<!-- skip -->
```python
# Flask
from flask import Flask
from python_wrap.web import WsgiIdempotencyMiddleware, flask_register_error_handlers

app = Flask(__name__)
app.wsgi_app = WsgiIdempotencyMiddleware(app.wsgi_app, scope=lambda env: env.get("HTTP_X_TENANT", ""))
flask_register_error_handlers(app)
```

<!-- skip -->
```python
# Django: wsgi.py
from django.core.wsgi import get_wsgi_application
from python_wrap.web import WsgiIdempotencyMiddleware

application = WsgiIdempotencyMiddleware(get_wsgi_application())

# In a view or middleware, turn a rejection into a response:
#   except (RateLimitExceeded, CircuitOpenError, BulkheadFull) as exc:
#       return django_response(exc)
```

## Rate-limit and rejection responses

```python
from python_wrap.errors import BulkheadFull, CircuitOpenError, RateLimitExceeded
from python_wrap.web import problem_response, ratelimit_headers, too_many_requests

headers = ratelimit_headers(limit=100, remaining=7, reset=12.3, policy="100;w=60")
assert headers["RateLimit-Reset"] == "13"          # seconds, rounded up

status, headers, body = too_many_requests(RateLimitExceeded("slow down", retry_after=2.1), limit=100)
assert status == 429 and headers["Retry-After"] == "3"

status, headers, _ = problem_response(CircuitOpenError("open", retry_after=4.5))
assert status == 503 and headers["Retry-After"] == "5"

status, headers, _ = problem_response(BulkheadFull("full", name=None, max_concurrent=4))
assert status == 503 and headers["Retry-After"] == "1"
```

`problem_response(exc)` maps `RateLimitExceeded` to 429, and `CircuitOpenError` and `BulkheadFull` to 503. Bodies are RFC 9457 `application/problem+json`; the rate-limit headers follow the IETF `RateLimit-*` draft.

## Limitations

The middleware prevents duplicate execution of a request while a result is retained, but it does not give exactly-once side effects across crashes: if the process dies after the handler wrote to an external system and before the response was stored, a retry runs the handler again. Use a transactional outbox or a database-level idempotency key for that guarantee. See [limitations](../project/limitations.md).
