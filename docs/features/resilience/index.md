# Resilience

Calls to networks, disks and other services fail, stall or get slow. These decorators decide what happens next: try again, stop calling a broken dependency, stop waiting, race a backup request, or return a sensible default.

<div class="grid cards" markdown>

-   :material-restart:{ .lg .middle } **[Retry](retry.md)**

    ---

    Call again when it raises or returns a bad value, with backoff, jitter, `Retry-After`, a time budget and a shared retry budget.

-   :material-electric-switch:{ .lg .middle } **[Circuit breaker](circuit-breaker.md)**

    ---

    Reject calls at once while a dependency is down, then probe for recovery.

-   :material-timer-sand:{ .lg .middle } **[Timeout and deadline](timeout.md)**

    ---

    Limit one call or a block of code, or give a whole request one shared budget.

-   :material-lifebuoy:{ .lg .middle } **[Fallback and suppress](fallback.md)**

    ---

    Return a default value, or `None`, instead of raising.

-   :material-call-split:{ .lg .middle } **[Hedge](hedge.md)**

    ---

    Start a backup attempt when a call is slow and take the first success.

-   :material-layers-triple:{ .lg .middle } **[Policies](policies.md)**

    ---

    Bundle several of these into one named, reusable decorator, in the right order.

</div>

## Which one?

| Your problem | Use |
| --- | --- |
| Errors that usually go away if you try again (dropped connection, 503) | [`retry`](retry.md) |
| A dependency that is down and keeps failing | [`circuit_breaker`](circuit-breaker.md) |
| A call that can hang or run too long | [`timeout` / `deadline`](timeout.md) |
| Rare slow calls (tail latency) on a replicated service | [`hedge`](hedge.md) |
| A failure that should become a default value | [`fallback` / `suppress`](fallback.md) |
| All of the above on one call, in the right order | [`policy`](policies.md) |

These combine well, and order matters: the usual stack is fallback outside, then circuit breaker, then retry, then timeout inside. [Policies](policies.md) apply that order for you, and [Stacking](../../get-started/stacking.md) explains why.
