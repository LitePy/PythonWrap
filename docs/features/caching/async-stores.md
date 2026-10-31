# Async cache stores

`memoize(store=RedisStore(...))` calls its store synchronously. On an asyncio event
loop (a FastAPI service, say) every cache read and write is then a blocking network
round trip that stalls every other request. `amemoize` is the native async path: its
stores are coroutines, so Redis I/O awaits instead of blocking.

## A runnable example

`AsyncMemoryStore` is the default store. Concurrent misses for the same key share one
computation (`singleflight`), exceptions are never cached, and cancelling one waiter does
not cancel the shared work for the others.

```python
import asyncio
from python_wrap.async_cache import amemoize

executions = 0

@amemoize(ttl=60)
async def load_profile(user_id: int) -> dict:
    global executions
    executions += 1
    await asyncio.sleep(0.01)
    return {"id": user_id}

async def main() -> None:
    results = await asyncio.gather(*(load_profile(1) for _ in range(10)))
    assert results == [{"id": 1}] * 10
    assert executions == 1
    assert load_profile.cache_info().hits == 9
    await load_profile.cache_delete(1)
    await load_profile.cache_clear()

asyncio.run(main())
```

Options: `ttl`, `key`, `store`, `cache_none`, `copy_result` and `singleflight`. The
wrapper exposes `await cache_clear()`, `await cache_delete(*args, **kwargs)` and
`cache_info()`. `amemoize` raises `TypeError` for non-async functions; use `memoize`
there. It emits the same `cache.hit` and `cache.miss` events as `memoize`, and honours
`python_wrap.testing.disable_caching()` and `fake_clock`.

## Redis with FastAPI

`AsyncRedisStore` takes a `redis.asyncio` client. python-wrap does not import or install
`redis`. Use a separate prefix for each cached function.

<!-- skip -->
```python
import redis.asyncio as redis
from fastapi import FastAPI
from python_wrap.async_cache import AsyncRedisStore, amemoize, json_codec

client = redis.Redis(socket_timeout=2, socket_connect_timeout=2)
dumps, loads = json_codec()
store = AsyncRedisStore(client, "myapp:price:v1:", dumps=dumps, loads=loads)

app = FastAPI()

@amemoize(ttl=300, store=store)
async def price(sku: str) -> dict:
    return await load_price(sku)

@app.get("/price/{sku}")
async def get_price(sku: str) -> dict:
    return await price(sku)
```

By default errors from Redis are logged and treated as misses (`fail_open=True`); pass
`fail_open=False` to raise. `clear()` scans only this store's prefix and deletes in
batches with `UNLINK`.

## Serialization and trust

Entries are stored as `(value, expires_at)` pairs. The default serializer is pickle,
and unpickling bytes an attacker can write to Redis can execute arbitrary code, so use
pickle only with a Redis instance you fully trust. For anything else pass
`json_codec()`; values must then be JSON-serializable and tuples come back as lists.
