# Adding PythonWrap to an existing FastAPI service

PythonWrap can standardize resilience controls and diagnostics around service functions.
It does not speed up SQL queries or replace the application's delivery guarantees.
Start with one bounded integration and preserve the existing response contract.

## Where it helps

| Typical application code | Recommended use | Boundary to preserve |
| --- | --- | --- |
| An in-process job retry helper with a fixed schedule such as three tries and `[2, 10, 60]` | `retrying(max_attempts=3, delays=[2, 10, 60])`, counters and hooks | The two actual waits remain 2 and 10 seconds; preserve the final exception and the existing task owner. |
| An outbound HTTP module that deliberately logs only error classes | `log_values=False` on any retry around a credential-bearing operation | Do not expose exception URLs, response bodies or tracebacks through retry logs. |
| A shared HTTP transport/pool used by several API callers | Wrap the existing HTTP call with timing or a narrowly scoped read retry | Reuse its transport, timeouts, proxy handling and shutdown. Do not add a second client pool. |
| A per-tenant lookup that copies results and scopes them by tenant/credential identity | Opt-in cache copy isolation and an explicit tenant/credential-aware key | Keep authentication, tenant partitioning, credential rotation, TTL and force-refresh behavior. |
| Request-context middleware that already binds request/user/client identifiers | Keep the existing middleware; optionally bind PythonWrap context around selected service work | Avoid competing request IDs or deriving tenant identity from untrusted headers. |
| A durable outbox with owner-checked delivery leases | Keep durable scheduling and compensation there; instrument the delivery operation | An in-process retry must not replace persisted retry times, crash recovery or uncertain-start compensation. |
| A database-backed cache or stampede locks | Keep the current cache and locks | Preserve serialized rows, namespaces, ownership checks, async sessions and transaction behavior. |

## Preserving the job delay contract

The package must be installed in the application environment before importing it. Use
the tested 0.3.0 wheel for evaluation; do not assume that an unreleased version exists
on PyPI. PythonWrap itself adds no runtime dependencies.

```python
import asyncio
from python_wrap import retrying
from python_wrap.testing import fake_clock

async def run_job(job, *, tries, backoff):
    # Preserve the legacy no-op for tries <= 0 and the zero-wait empty schedule.
    if tries <= 0:
        return
    async for attempt in retrying(
        max_attempts=tries,
        delays=tuple(backoff) or (0,),
        log_values=False,
        log_level=None,  # leave the application's existing retry/error logs in charge
    ):
        async with attempt:
            await job()

calls = []

async def job():
    calls.append(1)
    raise ConnectionError("temporarily unavailable")

with fake_clock() as clock:
    try:
        asyncio.run(run_job(job, tries=3, backoff=[2, 10, 60]))
    except ConnectionError:
        pass
    assert len(calls) == 3
    assert clock.sleeps == [2, 10]
```

This is an adoption example, not a patch to an existing retry helper. If that helper has an
injectable `sleep` test seam, it needs an explicit compatibility decision when migrating;
PythonWrap tests can use `fake_clock`. Keep the application's existing job safety
checks and final exception handling.

## Adapting the existing outbound response wrapper

Suppose the app's response wrapper exposes `status()` and `raw.headers`. Supply accessors rather than
changing the wrapper or adding an HTTP dependency to PythonWrap:

```python
import asyncio
from python_wrap import retry, retry_after_header

class Raw:
    headers = {"Retry-After": "0"}

class Response:
    raw = Raw()
    def __init__(self, status):
        self.code = status
    def status(self):
        return self.code

statuses = iter([503, 200])

@retry(
    max_attempts=2,
    on=ConnectionError,
    retry_if_result=lambda response: response.status() in {429, 502, 503, 504},
    delay_from=lambda response: retry_after_header(getattr(response, "raw", response)),
    log_values=False,
)
async def safe_read():
    # In the app, call the existing outbound helper and preserve its timeout/options.
    return Response(next(statuses))

assert asyncio.run(safe_read()).status() == 200
```

Only use this pattern for operations already safe to repeat. Signed requests must be
rebuilt inside each attempt when they require fresh timestamps/nonces. Payments, reservations, webhook consumption and durable outbox transitions retain their domain
idempotency and compensation rules. Local rate limiters/breakers are process-local;
shared state is explicit, and changing production rate limits needs a separate decision.

## Isolating cached response data

For a new in-memory cache, include authenticated scope and the credential/configuration
generation in its key. Use a nonsecret rotation identifier, and perform authorization
before consulting the cache. A cache key does not establish permission to read a tenant.

```python
from python_wrap import memoize

@memoize(
    ttl=30,
    copy_result=True,
    key=lambda tenant_id, credential_generation: (
        "catalog-v1", tenant_id, credential_generation
    ),
)
def catalog(tenant_id: int, credential_generation: int) -> list[dict]:
    return [{"id": "demo", "labels": ["available"]}]

first = catalog(7, 1)
first[0]["labels"].append("caller-only")
assert catalog(7, 1)[0]["labels"] == ["available"]
assert catalog.cache_info().hits == 1
catalog(8, 1)  # a different tenant gets a separate entry
catalog(7, 2)  # credential/configuration rotation gets a separate entry
assert catalog.cache_info().misses == 3
```

Copying costs CPU/memory and follows each object's Python deepcopy implementation.
Use it for data structures such as JSON-shaped lists/dicts, not sessions, transports
or objects deliberately implementing shared identity. Do not layer this example over
an existing database cache. An existing loader's force-refresh and dynamic TTL
would need their own contract tests before replacing it.

## Adoption checks

Before changing the app, start from a clean branch and pin the candidate package artifact. Run the affected existing tests, response-contract
and tenant-isolation checks, lint and dependency checks. For a retry migration, verify
attempt counts, exact waits, cancellation, final exception identity and safe logs.
Measure latency/overhead before claiming a performance improvement.

For an async cache backed by `redis.asyncio`, see
[async cache stores](../features/caching/async-stores.md); do not pass async clients or
sessions to the synchronous `CacheStore` interface.
