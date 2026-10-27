# Developer tools

Tools for evolving an API, faking data, and keeping decorator stacks tidy: `deprecate`, `mock_data`, `compose`, `wrap_methods` and `no_debug`.

```python
import warnings
from python_wrap import deprecate

@deprecate(since="0.2", removed_in="1.0", alternative="new_func")
def old_func() -> int:
    return 1

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    assert old_func() == 1

assert caught[0].category is DeprecationWarning
text = str(caught[0].message)
assert "old_func" in text and "0.2" in text and "1.0" in text and "new_func" in text
```

## When to use it

- `deprecate` to retire a function, method or class without surprising callers.
- `mock_data` to return fixed data in tests and local development.
- `compose` to define a decorator stack once and reuse it.
- `wrap_methods` to decorate many methods of a class in one line.

## `deprecate`

Mark a function, method or class as deprecated. Every call (or, for `async def`, every await) emits a warning that **points at the caller's line**, not at the library, so users see which of their code to change. The wrapped object also gets a `__deprecated__` attribute (PEP 702).

The structured parts are combined into `"<name> is deprecated since ... and will be removed in ...; use ... instead."`. Pass a message to replace the generated text, and `category=` to change the warning class:

=== "Message"

    ```python
    import warnings
    from python_wrap import deprecate

    @deprecate("fetch() is going away; call get() instead")
    def fetch() -> None: ...

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fetch()
    assert str(caught[0].message) == "fetch() is going away; call get() instead"
    ```

=== "Class"

    ```python
    import warnings
    from python_wrap import deprecate

    @deprecate("Use Widget2 instead")
    class Widget:
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        widget = Widget()

    assert isinstance(widget, Widget)          # still a class, isinstance still works
    assert "Widget2" in str(caught[0].message)
    ```

=== "Category"

    ```python
    import warnings
    from python_wrap import deprecate

    @deprecate(since="0.2", category=FutureWarning)
    def shown_to_end_users() -> None: ...

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        shown_to_end_users()
    assert caught[0].category is FutureWarning
    ```

Classes are changed in place: their `__init__` warns.

!!! note "Who sees the warning"
    Python hides `DeprecationWarning` by default outside `__main__` and test runners. Use `category=FutureWarning` when end users, not developers, should see it.

## `mock_data`

Replace a function's result with fixed data, for tests and local development. When enabled, the real function is **not called**.

```python
import os
from python_wrap import mock_data

@mock_data({"id": 1, "name": "Ada"}, enabled=lambda: os.environ.get("USE_MOCKS") == "1")
def fetch_user(user_id: int) -> dict:
    raise RuntimeError("would call the real API")

os.environ["USE_MOCKS"] = "1"
assert fetch_user(99) == {"id": 1, "name": "Ada"}

os.environ["USE_MOCKS"] = "0"
try:
    fetch_user(99)
except RuntimeError:
    pass   # disabled: the real function ran
else:
    raise AssertionError("expected the real function to run")
```

- `enabled` is `True`, `False`, or a zero-argument callable evaluated **on every call**, so it can read an environment variable or a settings flag.
- If `data` is callable it is called on each call and its result is returned. To return a callable itself, wrap it: `mock_data(lambda: my_fn)`.
- Works for `async def` functions (the awaited result is the data).

```python
import asyncio
from python_wrap import mock_data

@mock_data(lambda: [1, 2, 3])   # a fresh list on every call
async def numbers() -> list[int]:
    raise RuntimeError

assert asyncio.run(numbers()) == [1, 2, 3]
```

!!! danger "Never ship it enabled"
    `mock_data` silently replaces real behaviour. Tie `enabled=` to an explicit opt-in such as an environment variable that is off by default, never a default of `True` in code that reaches production. In tests, see [Testing](../testing/index.md).

## `compose`

Combine decorators into one. `compose(a, b, c)(f)` is exactly `a(b(c(f)))`, so list them outermost first, as if stacked top to bottom.

```python
from python_wrap import compose, memoize, retry, validate_args

resilient_lookup = compose(
    memoize(ttl=60),
    retry(max_attempts=3, delay=0.01),
    validate_args(key=lambda k: k != ""),
)

@resilient_lookup
def lookup(key: str) -> str:
    return key.upper()

@resilient_lookup
def lookup_other(key: str) -> str:
    return key.lower()

assert lookup("a") == "A"
assert lookup_other("A") == "a"
```

Define a stack once and reuse it across your codebase. `compose()` with no decorators returns the function unchanged, and a non-callable argument raises `TypeError`. The order rules in [Stacking decorators](../get-started/stacking.md) apply to the arguments.

!!! note "Share state deliberately"
    `compose(memoize(...))` creates its `memoize` decorator once, but `memoize` still gives each decorated function its own cache. A `CircuitBreaker` instance, on the other hand, is shared by everything it decorates. That is usually what you want for a breaker, but check it.

## `wrap_methods`

Apply decorators to the methods of a class in one line. Decorators are listed outermost first, like `compose`, and a [policy](resilience/policies.md) works as well.

```python
import asyncio
from python_wrap import policy, retry, timeout, wrap_methods

calls = []

@wrap_methods(
    policy(retry=retry(max_attempts=2, delay=0)),
    exclude=["close"],
)
class Client:
    def get(self, key: str) -> str:
        calls.append(key)
        return key

    async def fetch(self, key: str) -> str:
        return key

    @staticmethod
    def parse(raw: str) -> int:
        return int(raw)

    def close(self) -> None: ...

client = Client()
assert client.get("a") == "a"
assert asyncio.run(client.fetch("b")) == "b"
assert Client.parse("3") == 3
```

Plain, `async def`, `@staticmethod` and `@classmethod` methods are decorated correctly (the underlying function is wrapped and put back in its descriptor). By default these are skipped: dunder methods, names starting with `_`, properties, non-function attributes and inherited methods.

| Option | Meaning |
| --- | --- |
| `include` | Names (list/set) or a predicate. Restricts the selection. Naming a dunder or a property here opts it in. |
| `exclude` | Names or a predicate to leave alone. Wins over `include`. |
| `include_private` | Also select `_single_underscore` methods. |

## `no_debug`

A deprecated no-op kept so v0.1 code keeps importing. It returns the function unchanged and warns when applied. It will be removed in 0.3. To hide debug output, configure [logging levels](observability/logging.md#configuring-logging) instead.

## See also

- [Stacking decorators](../get-started/stacking.md)
- [Policies](resilience/policies.md)
- [Testing](../testing/index.md)
- [API reference](../api/devtools.md)
