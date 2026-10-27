# Caching and deduplication

These decorators avoid doing the same work twice: remembering a result, running setup once, computing an attribute lazily, sharing one in-flight call between duplicate callers, or turning many small lookups into one bulk call.

<div class="grid cards" markdown>

-   :material-memory:{ .lg .middle } **[Memoize](memoize.md)**

    ---

    Cache a function's result per argument values, with LRU size limits, TTL and cache control.

-   :material-numeric-1-circle-outline:{ .lg .middle } **[Once](once.md)**

    ---

    Run a function a single time; later calls return the first result.

-   :material-tag-text-outline:{ .lg .middle } **[Cached property](cached-property.md)**

    ---

    A per-instance lazy attribute with TTL, async getters and invalidation.

-   :material-package-variant-closed:{ .lg .middle } **[Batching](batching.md)**

    ---

    Many single-item calls made close together become one bulk call.

-   :material-call-merge:{ .lg .middle } **[Singleflight](singleflight.md)**

    ---

    Identical concurrent calls share one execution.

</div>

## Which one?

| You want | Use |
| --- | --- |
| Same arguments give the same answer (a pure function) | [`@memoize`](memoize.md) (add `maxsize` so it cannot grow without bound) |
| The answer goes stale after a while | [`@memoize(ttl=...)`](memoize.md#expiry-and-size) |
| One result regardless of arguments (init, setup, config load) | [`@once`](once.md) |
| An expensive attribute of one object | [`@cached_property`](cached-property.md) |
| Collapse duplicate concurrent calls (no cache) | [`@singleflight`](singleflight.md) |
| Many single lookups folded into one bulk call | [`@batch_calls`](batching.md) |

They combine: for example, stack `singleflight` under `memoize` so that two threads missing the same key compute the value only once.

## See also

- [API reference](../../api/caching.md)
- [Metrics and events](../observability/metrics.md): `cache.hit`, `cache.miss` and `cache.evicted`
