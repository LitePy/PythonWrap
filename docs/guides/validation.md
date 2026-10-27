# Validation

Reject bad input at the door, with an error that names the parameter.

| Decorator | Checks | Raises |
| --- | --- | --- |
| [`validate_args`](#validate_args) | Your predicate per argument | `ValidationError` (a `ValueError`) |
| [`check_type`](#check_type) | `isinstance` per argument | `ArgumentTypeError` (a `TypeError`) |
| [`enforce_types`](#enforce_types) | Arguments and return value against the function's own annotations | `ArgumentTypeError` (a `TypeError`) |

All three work on plain functions, `async def` functions and methods, can be stacked, and keep the wrapped function's signature. A parameter name that does not exist raises `TypeError` **when you decorate**, so typos fail at import time rather than in production.

## `validate_args`

Give each parameter a predicate. A falsy result raises `ValidationError`.

=== "Sync"

    ```python
    from python_wrap import ValidationError, validate_args

    @validate_args(
        quantity=lambda q: q > 0,
        sku=(lambda s: s.startswith("SKU-"), "sku must start with 'SKU-'"),
    )
    def order(sku: str, quantity: int) -> str:
        return f"{quantity} x {sku}"

    assert order("SKU-1", 2) == "2 x SKU-1"

    try:
        order("SKU-1", 0)
    except ValidationError as exc:
        assert exc.parameter == "quantity" and exc.value == 0
        assert isinstance(exc, ValueError)

    try:
        order("bad", 1)
    except ValidationError as exc:
        assert "must start with" in str(exc)
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import ValidationError, validate_args

    @validate_args(n=lambda n: n > 0)
    async def double(n: int) -> int:
        return n * 2

    async def main() -> None:
        assert await double(2) == 4
        try:
            await double(-1)
        except ValidationError:
            pass
        else:
            raise AssertionError("expected a ValidationError")

    asyncio.run(main())
    ```

Details:

- Use a `(predicate, "message")` tuple for a custom message.
- Default values are applied first, so defaults are validated too.
- A validator may raise its own exception instead of returning falsy; it propagates unchanged.
- For `*args` / `**kwargs` parameters the validator receives the whole tuple / dict.

## `check_type`

`isinstance` checks, one per argument. The value can be a class or a tuple of classes.

```python
from python_wrap import ArgumentTypeError, check_type

@check_type(x=int, label=(str, bytes))
def tag(x, label):
    return f"{label}:{x}"

assert tag(1, "a") == "a:1"

try:
    tag("1", "a")
except ArgumentTypeError as exc:
    assert exc.parameter == "x" and exc.expected is int and exc.value == "1"
    assert isinstance(exc, TypeError)
```

Only arguments that are actually passed are checked (defaults are not). For `*args` / `**kwargs` the type applies to every element / value.

## `enforce_types`

Check the arguments and the return value against the function's own annotations. No extra configuration: your type hints become runtime checks.

=== "Sync"

    ```python
    from python_wrap import ArgumentTypeError, enforce_types

    @enforce_types
    def total(items: list[int], scale: float = 1.0) -> float:
        return sum(items) * scale

    assert total([1, 2, 3]) == 6.0

    try:
        total([1, 2, "3"])
    except ArgumentTypeError as exc:
        assert exc.parameter == "items"
        assert "index 2" in str(exc)
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import ArgumentTypeError, enforce_types

    @enforce_types
    async def name_length(name: str) -> int:
        return len(name)

    async def main() -> None:
        assert await name_length("ana") == 3
        try:
            await name_length(42)  # type: ignore[arg-type]
        except ArgumentTypeError:
            pass
        else:
            raise AssertionError("expected an ArgumentTypeError")

    asyncio.run(main())
    ```

The error message says what was expected and what was found, for example `parameter 'items' expected list[int], got list containing str at index 2`. For a bad return value `error.parameter` is `"return"`; turn that off with `@enforce_types(check_return=False)`.

```python
from python_wrap import ArgumentTypeError, enforce_types

@enforce_types
def broken() -> int:
    return "not an int"  # type: ignore[return-value]

try:
    broken()
except ArgumentTypeError as exc:
    assert exc.parameter == "return"
```

Annotations are resolved lazily on the first call with `typing.get_type_hints`, so forward references work. For async functions the awaited result is checked.

### Supported annotations

| Annotation | How it is checked |
| --- | --- |
| Plain classes (`int`, `str`, your own) | `isinstance`. `int` also accepts `bool`; `float` accepts `int`; `complex` accepts `int` and `float` (PEP 484). |
| `Any`, `None` | Anything / `None` only. |
| `Optional[T]`, `Union[A, B]`, PEP 604 unions (the `X` bar `Y` syntax) | Any member matches. |
| `Literal[...]` | The value is one of the literals. |
| `list[T]`, `set[T]`, `frozenset[T]`, `tuple[T, ...]`, `tuple[A, B]`, `dict[K, V]` | Container type **and each element**. |
| `Sequence[T]`, `MutableSequence[T]`, `Set[T]`, `Mapping[K, V]` | ABC and each element. |
| `Iterable[T]`, `Iterator[T]`, `Generator` | `isinstance` only. Iterators are never consumed. |
| `Callable[...]` | `callable()` only. |
| `type[T]` | The value is a subclass of `T`. |
| `TypeVar` | Its bound or constraints; unbound means anything. |
| `Annotated[T, ...]` | Checks `T`. |
| `NewType` | Checks the supertype. |
| `runtime_checkable` protocols | `isinstance`. |
| `TypedDict`, non-runtime-checkable protocols, unresolved forward references, anything else | **Accepted without checking.** |

A check that cannot be performed never raises: unsupported annotations are accepted rather than rejected.

```python
from typing import Annotated, Literal, Optional
from python_wrap import ArgumentTypeError, enforce_types

@enforce_types
def configure(
    mode: Literal["fast", "safe"],
    retries: Optional[int] = None,
    tags: dict[str, list[int]] | None = None,
    level: Annotated[int, "0 to 9"] = 0,
) -> None: ...

configure("fast", tags={"a": [1, 2]})

for bad in ({"mode": "turbo"}, {"mode": "fast", "tags": {"a": ["x"]}}):
    try:
        configure(**bad)
    except ArgumentTypeError:
        pass
    else:
        raise AssertionError(f"{bad} should have been rejected")
```

!!! warning "A runtime cost"
    Checking a `list[int]` inspects every element on every call. Use `enforce_types` at boundaries (public API, request handlers), not inside hot loops.

## Choosing between them

- Business rules (`amount > 0`, "not empty", "valid email"): `validate_args`.
- "This must be an `int` or `str`": `check_type` is the lightest.
- "My annotations should be true at runtime": `enforce_types`.

They stack: `enforce_types` for shape, `validate_args` for rules.

```python
from python_wrap import enforce_types, validate_args

@enforce_types
@validate_args(amount=(lambda a: a > 0, "amount must be positive"))
def charge(account: str, amount: float) -> str:
    return f"{account}: {amount}"

assert charge("acme", 5) == "acme: 5"
```
