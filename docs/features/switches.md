# Dynamic switches

Disable a misbehaving integration without rebuilding its decorator stack. A switch is
read when the function is called; `ttl=` optionally caches source reads locally.

```python
import os
from python_wrap import EnvSource, switch

@switch("catalog", source=EnvSource(), when_off="skip")
def fetch_catalog():
    return "catalog"

os.environ["PW_SWITCH_CATALOG"] = "false"
assert fetch_catalog() is None
os.environ["PW_SWITCH_CATALOG"] = "true"
assert fetch_catalog() == "catalog"
del os.environ["PW_SWITCH_CATALOG"]
```

`EnvSource` uses the prefix `PW_SWITCH_` and uppercases the name. True values are
`1`, `true`, `yes`, `on`, or `enabled`; false values are `0`, `false`, `no`, `off`, or
`disabled`, case-insensitively. Missing values use `default=True`, which can be changed
on the source. Invalid strings raise ValueError rather than silently enabling work.

`RedisSource(client, prefix=..., default=...)` reads the same boolean format from Redis.
It needs the application's existing synchronous Redis client. Async wrappers offload
source reads to a thread. Source errors propagate; choose an explicit fallback policy
if your application needs a different response to configuration-store outages.

`when_off="raise"` (default) raises `FeatureDisabled`; `"skip"` returns None. A callable
fallback receives the original arguments; an async wrapper awaits an awaitable fallback.
`ttl=0` reads every time. A positive TTL trades faster reads for a bounded delay before
changes take effect, separately in each process.

See [switches API](../api/switches.md).
