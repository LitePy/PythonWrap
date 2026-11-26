# Named policies and retry predicates

Use an immutable `policy(...)` for a fixed stack. Use `PolicyRegistry` or the global
`policies` registry when settings need a name and may change while the process runs.

## Register and resolve by name

```python
from python_wrap import PolicyRegistry

registry = PolicyRegistry()
registry.register("catalog", retry={"max_attempts": 2, "delay": 0},
                  breaker={"failure_threshold": 5}, timeout=2)

@registry.resilient("catalog")
def load_catalog() -> str:
    return "ready"

assert load_catalog() == "ready"
assert registry.describe("catalog")["catalog"]["order"] == ("breaker", "retry", "timeout")

registry.register("catalog", retry={"max_attempts": 1, "delay": 0})
assert registry.describe("catalog")["catalog"]["generation"] == 2
```

A decorated function resolves the current configuration at call time. Its compiled stack
is reused until the registry generation changes. Functions using the same name share
breaker, limiter and bulkhead state within that generation. A changed policy gets a new
generation. An already running call keeps its old stack. Register settings before the
first call, or an unknown name raises KeyError.

When a reload keeps the policy name, an open in-process breaker stays open and uses the
new thresholds. Limiters and bulkheads start fresh in the new generation. Shared breakers
keep their state in the backend anyway. An unchanged policy keeps its generation and its
live objects.

`policies.register(...)` and the top-level `@resilient("name")` use the shared registry.
Supported configuration roles are `fallback`, `cache`, `limiter`, `bulkhead`, `breaker`,
`retry`, and `timeout`. Their relative order is the same as [Policy](policies.md):
cache/limiter/bulkhead/breaker/retry/timeout, with fallback outside them.

`load_from_mapping()` validates a complete mapping before applying it. YAML parsing is
left to the application; pass the resulting mapping. `load_from_env(prefix="PW_POLICY_")`
reads each matching environment variable as a JSON policy object. Names are case-sensitive:
`PW_POLICY_CATALOG` defines `CATALOG`, not `catalog`. `describe()` exposes role order and
generation without printing client objects or credentials.

<!-- skip -->
```python
from python_wrap import policies

# For example: PW_POLICY_CATALOG='{"retry":{"max_attempts":3,"delay":0.5}}'
policies.load_from_env()
```

## Live changes from a source

A source is any object with `read()` that returns a mapping. `EnvSource(prefix="PW_POLICY_")`,
`FileSource(path)` (a JSON file shaped `{"name": {settings}}`) and
`RedisSource(client, key="python-wrap:policies")` (one JSON string key shared by every
replica) are built in. `policies.watch(source, interval=30)` reads the source now and then
every `interval` seconds in a daemon thread. It returns a `Watch` with `poll()` (read once,
returns True if a change was applied) and `stop()`.

The whole mapping is validated before it is applied. A bad mapping (unreadable, invalid JSON,
unknown role or name) keeps the current policies and emits `config.rejected` with an `error`
field. Each applied policy emits `config.changed` with a `generation` field.

Mappings refer to objects by name, never by import path. Exception names go in `"on"` and
`"exclude"` (`"builtins.ConnectionError"`). Predicate names go in `"retry_if"` and
`"retry_if_result"` (`"db_transient"`). A string starting with `@` names a backend
(`"backend": "@default"`). Register your own names on the registry before the watch
applies them:

```python
policies.register_exception("vendor.Timeout", VendorTimeout)
policies.register_predicate("vendor_transient", fn)
policies.register_backend("default", RedisWindow(...))
```

Every builtin exception and the `*_transient` predicates from `python_wrap.predicates` are
registered already. An unknown name raises ValueError that names the `register_*` call to use.

A watch does not survive a fork. Start it in each worker after the fork.

`python_wrap.health.health.report()` includes a `policies` entry with each policy's
generation once the global `policies` registry has loaded one.

<!-- skip -->
```python
from redis import Redis
from python_wrap.policies import RedisSource, policies, resilient

policies.register_predicate("vendor_transient", lambda exc: getattr(exc, "status", 0) >= 500)
watch = policies.watch(RedisSource(Redis()), interval=30)
# Redis key "python-wrap:policies":
# {"vendor": {"limiter": {"calls": 50, "period": 1},
#             "retry": {"max_attempts": 3, "retry_if": "vendor_transient"},
#             "breaker": {"failure_threshold": 5, "on": ["builtins.ConnectionError"]}}}

@resilient("vendor")
def fetch_quote(sku: str) -> dict: ...
```

## Composable exception predicates

`Predicate` supports short-circuit `|`, `&`, and `~`. `in_cause_chain` checks the exception,
its cause and context, and a driver's `.orig` chain, while guarding against cycles.

```python
from python_wrap import in_cause_chain, message_contains, sqlstate

is_transient = in_cause_chain(sqlstate("08") | message_contains("deadlock"))
original = Exception("08S01", "connection lost")
wrapped = RuntimeError("driver wrapper")
wrapped.__cause__ = original
assert is_transient(wrapped)
assert not (is_transient & message_contains("fatal"))(wrapped)
```

`sqlstate()` reads `.sqlstate`, `.pgcode`, or a string in DB-API `args[0]`. Prefixes such
as `08` match a SQLSTATE class; a full code such as `40001` matches that code. Drivers are
not imported. Keep your application's classification rules explicit: a generic message
match can be broader than a driver's structured code.

## Transient database errors

Ready-made predicates classify the errors worth retrying for each driver family. They
read error codes and attributes only, follow the cause chain and SQLAlchemy's `.orig`,
and never import a driver.

| Predicate | Matches |
| --- | --- |
| `postgres_transient` | SQLSTATE `08xxx`, `40001`, `40P01`, `53300`, `55P03`, `57P01`-`57P03` (psycopg 2/3, asyncpg) |
| `mysql_transient` | Error numbers 1040, 1205, 1213, 1290, 1836, 1927, 2002, 2003, 2006, 2013, 2055, 4031 (PyMySQL, mysqlclient, aiomysql, asyncmy) |
| `sqlserver_transient` | ODBC SQLSTATE `08xxx`, `40001`, `HYT00`/`HYT01` and native numbers such as 1205 (deadlock), 1222, 40613/40197/40501 (Azure SQL), 10928/10929, 10053/10054 (pyodbc, pymssql) |
| `db_transient` | Any of the above, or a SQLAlchemy error with `connection_invalidated=True` |
| `error_code(*numbers)` | Your own list of driver error numbers |

```python
from python_wrap import db_transient, retry
from python_wrap.testing import fake_clock


class OperationalError(Exception):
    pass


OperationalError.__module__ = "pyodbc"  # what a real pyodbc error reports
calls = []


@retry(max_attempts=3, delay=0.2, retry_if=db_transient)
def save_rows() -> str:
    calls.append(1)
    if len(calls) == 1:
        raise OperationalError("40001", "[40001] Transaction was deadlocked (1205)")
    return "saved"


with fake_clock():
    assert save_rows() == "saved"
assert len(calls) == 2
```

Retry the **unit of work**, not a statement inside an open transaction: after a deadlock
or a dropped connection the transaction is gone, so the retried function must open a
new session or transaction itself. Code that already retries a whole file or batch at a
higher level should keep a single retry layer.

See [policies registry API](../../api/policies.md), [predicates API](../../api/predicates.md),
and [retry](retry.md#honouring-retry-after).
