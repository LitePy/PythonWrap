# HTTP clients

Retrying an HTTP call is not the same as retrying a function. A `POST /payments` that timed out may have been charged; a `429` carries a `Retry-After` that should drive the wait; a body-less `GET` is always safe. `python_wrap.http` encodes those rules once and plugs them into **httpx**, **requests** and any hand-written or aiohttp-style client, with no dependency on any of them.

```python
from python_wrap.http import HttpRetryPolicy, RetryTransport

class Url:
    host = "api.example.com"

class Request:
    method = "GET"
    headers: dict[str, str] = {}
    url = Url()

class Response:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}

class Flaky:
    """Stands in for httpx.HTTPTransport: fails twice, then succeeds."""
    def __init__(self):
        self.statuses = [503, 502, 200]

    def handle_request(self, request):
        return Response(self.statuses.pop(0))

    def close(self):
        pass

transport = RetryTransport(Flaky(), policy=HttpRetryPolicy(delay=0, jitter=0))
assert transport.handle_request(Request()).status_code == 200
```

## Method safety

A request is retried only if its method is idempotent (`GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`, `TRACE`) **or** it carries an `Idempotency-Key` header. A `POST` or `PATCH` without the header is never retried, neither on a `503` nor on a connection error, because the server may already have acted on it.

```python
from python_wrap.http import should_retry

assert should_retry("GET", {}, status=503)
assert not should_retry("POST", {}, status=503)
assert not should_retry("POST", {}, exc=ConnectionError())
assert should_retry("POST", {"Idempotency-Key": "order-42"}, status=503)
assert not should_retry("GET", {}, status=404)
```

`should_retry` and `retry_delay` are the pure decision functions every integration uses, handy for your own clients and for tests.

## The policy

`HttpRetryPolicy` is a frozen dataclass:

| Field | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `3` | Total tries including the first |
| `delay`, `backoff`, `max_delay`, `jitter` | `0.5`, `2.0`, `30`, `0` | Exponential backoff between tries |
| `statuses` | `RETRYABLE_STATUSES` | 408, 425, 429, 500, 502, 503, 504 |
| `methods` | `IDEMPOTENT_METHODS` | Methods safe to repeat |
| `retry_on` | `ConnectionError`, `TimeoutError`, `OSError` | Transport error types |
| `retry_on_names` | httpx / requests / aiohttp class names | Matched along the exception's MRO, so the libraries are never imported |
| `respect_retry_after` | `True` | Use `Retry-After` as the exact wait |
| `max_retry_after` | `60` | A longer `Retry-After` means: stop and hand back the response |
| `budget` | `None` | A shared [`RetryBudget`](retry.md) |
| `idempotency_header` | `"Idempotency-Key"` | Header that makes any method retryable (`None` disables) |

## httpx

Wrap the transport. `RetryTransport` is for `httpx.Client`, `AsyncRetryTransport` for `httpx.AsyncClient`. Responses that are discarded to retry are closed first.

<!-- skip -->
```python
import httpx
from python_wrap.http import AsyncRetryTransport, HttpRetryPolicy, RetryTransport

policy = HttpRetryPolicy(max_attempts=4, delay=0.2)

client = httpx.Client(transport=RetryTransport(httpx.HTTPTransport(), policy=policy))
aclient = httpx.AsyncClient(
    transport=AsyncRetryTransport(httpx.AsyncHTTPTransport(), policy=policy)
)
```

Request bodies must be replayable (bytes, str, JSON, form data), not one-shot streams.

## requests

`mount_retries` imports `requests` lazily, builds an `HTTPAdapter` subclass whose `send` runs the same loop, mounts it, and returns it.

<!-- skip -->
```python
import requests
from python_wrap.http import HttpRetryPolicy, mount_retries

session = requests.Session()
mount_retries(session, HttpRetryPolicy(max_attempts=4))
session.get("https://api.example.com/items")
```

## aiohttp and hand-written clients

`http_retry` decorates any function or coroutine that returns an object with `.status` or `.status_code` and `.headers`. Declare the method, since the function's arguments are opaque. A non-idempotent method gets no retries unless you assert `idempotent=True`.

```python
from python_wrap.http import HttpRetryPolicy, http_retry

class Resp:
    def __init__(self, status):
        self.status = status
        self.headers = {}

answers = [503, 503, 200]

@http_retry(HttpRetryPolicy(delay=0))
def fetch():
    return Resp(answers.pop(0))

assert fetch().status == 200

@http_retry(method="POST")  # no retries: not idempotent
def create():
    return Resp(503)

assert create().status == 503
```

<!-- skip -->
```python
import aiohttp
from python_wrap.http import http_retry

@http_retry(method="PUT")
async def put_item(session: aiohttp.ClientSession, item_id: int, data: dict):
    async with session.put(f"https://api.example.com/items/{item_id}", json=data) as resp:
        await resp.read()
        return resp
```

## Retry-After

When a response carries `Retry-After` (seconds or an HTTP date) that value is the wait, exactly, with no backoff or jitter added. If it exceeds `max_retry_after` the server is asking for longer than you are willing to wait, so retrying stops and the response is returned for the caller to handle.

```python
from python_wrap.http import HttpRetryPolicy, retry_delay

class Resp:
    def __init__(self, value):
        self.headers = {"Retry-After": value}

policy = HttpRetryPolicy(max_retry_after=30)
assert retry_delay(Resp("5"), 1, policy) == 5.0
assert retry_delay(Resp("3600"), 1, policy) is None
```

## Deadlines, budgets and events

- Inside a [`deadline`](timeout.md) block no retry sleeps past the time left; the last response is returned (or the last error raised) instead.
- Pass one `RetryBudget` as `policy.budget` to every client so an outage cannot multiply traffic by `max_attempts`. When it refuses, the last response or error stands.
- Events `retry.attempt_failed`, `retry.succeeded`, `retry.gave_up` and `retry.budget_exhausted` are emitted with the function name `http GET api.example.com` and the fields `attempt(s)`, `status`, `exception`, `next_delay`.

## Pairing with idempotency keys

To retry a write safely, send the same key on every attempt. The transports retry on the header automatically:

<!-- skip -->
```python
import uuid
client.post(
    "https://api.example.com/payments",
    json={"amount": 100},
    headers={"Idempotency-Key": str(uuid.uuid4())},
)
```

The server must deduplicate by that key. If it does not, leave writes unretried.
