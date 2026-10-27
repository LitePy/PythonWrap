# Get started

python-wrap is a set of decorators for the problems every project meets: calls that fail, work that repeats, load that needs limiting, and code you cannot see into. This section takes you from install to a working, tested resilient function. Read the pages in this order.

<div class="grid cards" markdown>

-   :material-download-outline:{ .lg .middle } __[Installation](installation.md)__

    ---

    pip, uv or poetry. Python 3.10+, no dependencies, typed.

-   :material-rocket-launch-outline:{ .lg .middle } __[Quickstart](quickstart.md)__

    ---

    A ten-minute tutorial: make a flaky client resilient, cached, observable and fast to test.

-   :material-compass-outline:{ .lg .middle } __[Choosing a decorator](choosing.md)__

    ---

    A table by problem: what to reach for, and what to combine it with.

-   :material-layers-triple-outline:{ .lg .middle } __[Stacking decorators](stacking.md)__

    ---

    Order changes behaviour. The rules, and a recommended order.

-   :material-lightning-bolt:{ .lg .middle } __[Async and methods](async-and-methods.md)__

    ---

    How every decorator behaves on `async def` functions and on methods.

-   :material-alert-octagon-outline:{ .lg .middle } __[Errors](errors.md)__

    ---

    The exceptions python-wrap raises, what each subclasses, and which decorator raises which.

</div>

## After this

- Browse the [features](../features/index.md) one decorator per page.
- Make your tests instant with the [testing helpers](../testing/index.md).
- Copy a complete setup from the [cookbook](../cookbook/index.md).
