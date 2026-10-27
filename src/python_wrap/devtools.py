"""Developer tools: deprecation warnings, mock data and decorator composition."""

from __future__ import annotations

import functools
import inspect
import warnings
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import P, R

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")

__all__ = ["compose", "deprecate", "mock_data", "no_debug", "wrap_methods"]


def _mark(obj: T, text: str) -> T:
    """Set the PEP 702 style ``__deprecated__`` attribute."""
    obj.__deprecated__ = text  # type: ignore[attr-defined]
    return obj


def _message_for(
    target: Any,
    message: str | None,
    since: str | None,
    removed_in: str | None,
    alternative: str | None,
) -> str:
    """Return an explicit message, or build one from the parts that were given."""
    if message is not None:
        return message
    func = target.__func__ if isinstance(target, (staticmethod, classmethod)) else target
    text = f"{_core.qualname(func)} is deprecated"
    if since:
        text += f" since {since}"
    if removed_in:
        text += f" and will be removed in {removed_in}"
    if alternative:
        text += f"; use {alternative} instead"
    return text + "."


def _wrap_class(cls: type, text: str, category: type[Warning]) -> type:
    """Wrap ``__init__`` (or ``__new__``) in place, so the class stays the same object.

    ``__new__`` is wrapped instead when the class has no ``__init__`` of its own but
    a custom ``__new__`` (``float`` subclasses, ``NamedTuple``...): installing an
    ``__init__`` there would make ``object.__init__`` reject the arguments.
    """
    current_new: Any = cls.__new__
    use_new = "__init__" not in cls.__dict__ and current_new is not object.__new__
    if use_new:
        original_new = current_new

        def warned_new(klass: Any, *args: Any, **kwargs: Any) -> Any:
            warnings.warn(text, category, stacklevel=2)
            return original_new(klass, *args, **kwargs)

        warned_new.__name__ = "__new__"
        warned_new.__qualname__ = f"{cls.__qualname__}.__new__"
        cls.__new__ = staticmethod(warned_new)  # type: ignore[method-assign,assignment]
        return _mark(cls, text)

    original_init: Any = cls.__init__  # type: ignore[misc]

    @functools.wraps(original_init)
    def warned_init(self: Any, *args: Any, **kwargs: Any) -> None:
        warnings.warn(text, category, stacklevel=2)
        original_init(self, *args, **kwargs)

    cls.__init__ = warned_init  # type: ignore[misc]
    return _mark(cls, text)


def _wrap_callable(func: Any, text: str, category: type[Warning]) -> Any:
    """Wrap a plain or async function so each call (or await) warns."""
    if _core.is_async(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(text, category, stacklevel=2)
            return await func(*args, **kwargs)

        return _mark(async_wrapper, text)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        warnings.warn(text, category, stacklevel=2)
        return func(*args, **kwargs)

    return _mark(wrapper, text)


def _wrap(target: Any, text: str, category: type[Warning]) -> Any:
    """Dispatch on the kind of object being deprecated."""
    if isinstance(target, (staticmethod, classmethod)):
        # Keep the descriptor type so the method still binds the same way.
        return type(target)(_wrap(target.__func__, text, category))
    if isinstance(target, type):
        return _wrap_class(target, text, category)
    return _wrap_callable(target, text, category)


@overload
def deprecate(target: F, /) -> F: ...
@overload
def deprecate(
    message: str | None = None,
    *,
    since: str | None = None,
    removed_in: str | None = None,
    alternative: str | None = None,
    category: type[Warning] = DeprecationWarning,
) -> Callable[[F], F]: ...
def deprecate(
    message: str | Callable[..., Any] | None = None,
    *,
    since: str | None = None,
    removed_in: str | None = None,
    alternative: str | None = None,
    category: type[Warning] = DeprecationWarning,
) -> Any:
    """Mark a function, method, or class as deprecated.

    Every call (or, for ``async def``, every await) emits a warning that points at
    the caller's line. Classes are changed in place: their ``__init__`` warns, and
    they stay classes, so ``isinstance`` keeps working.

    Works bare, with a message (the v0.1 form), or with structured parts. The parts
    are combined into ``"<name> is deprecated since ... and will be removed in ...;
    use ... instead."``. An explicit ``message`` replaces that generated text.

    Args:
        message: Full warning text. Replaces the generated text when given.
        since: Version in which the deprecation started.
        removed_in: Version in which the target will be removed.
        alternative: Name of the replacement to use instead.
        category: Warning class to emit.

    Returns:
        The decorated object when used bare, otherwise a decorator.

    Example:
        ```python
        @deprecate(since="0.2", removed_in="1.0", alternative="new_func")
        def old_func() -> None: ...

        @deprecate("Use Widget2 instead")
        class Widget: ...
        ```
    """
    if callable(message):
        text = _message_for(message, None, since, removed_in, alternative)
        return _wrap(message, text, category)

    def decorate(target: Any) -> Any:
        text = _message_for(target, message, since, removed_in, alternative)
        return _wrap(target, text, category)

    return decorate


def mock_data(data: Any, *, enabled: bool | Callable[[], bool] = True) -> Callable[[F], F]:
    """Replace a function's result with fixed data, for tests and local runs.

    When enabled, the wrapped function is not called and ``data`` is returned. If
    ``data`` is callable it is called on each call and its result is returned, so use
    ``lambda: fn`` to return a callable itself. When disabled, the real function runs.

    Args:
        data: Value to return, or a zero-argument callable producing it.
        enabled: ``True``/``False``, or a zero-argument callable that returns one.
            It is evaluated on every call, so it can read an environment variable.

    Returns:
        A decorator that works on plain and ``async def`` functions.

    Example:
        ```python
        @mock_data({"id": 1}, enabled=lambda: os.environ.get("MOCK") == "1")
        def fetch_user(user_id: int) -> dict[str, int]: ...
        ```
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        if _core.is_async(func):
            async_func = cast("Callable[P, Awaitable[Any]]", func)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                if _is_enabled(enabled):
                    return _mock_value(data)
                return await async_func(*args, **kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            if _is_enabled(enabled):
                return _mock_value(data)
            return func(*args, **kwargs)

        return cast("Callable[P, R]", wrapper)

    return cast("Callable[[F], F]", decorate)


def _is_enabled(enabled: bool | Callable[[], bool]) -> bool:
    return enabled() if callable(enabled) else enabled


def _mock_value(data: Any) -> Any:
    return data() if callable(data) else data


@overload
def no_debug(func: F, /) -> F: ...
@overload
def no_debug(func: None = None, /) -> Callable[[F], F]: ...
def no_debug(func: Callable[..., Any] | None = None, /) -> Any:
    """Deprecated no-op, kept only so v0.1 code keeps importing.

    It returns the function unchanged and emits a ``DeprecationWarning`` when applied.
    It will be removed in 0.3. To hide debug output, configure logging levels instead.

    Args:
        func: The function to return unchanged. Omit it to get a decorator.

    Returns:
        ``func`` itself, or an identity decorator when ``func`` is omitted.

    Example:
        ```python
        @no_debug  # warns: removed in 0.3, use logging levels instead
        def compute() -> int:
            return 1
        ```
    """
    warnings.warn(
        "no_debug() does nothing and will be removed in 0.3; "
        "configure logging levels instead (see logging.Logger.setLevel).",
        DeprecationWarning,
        stacklevel=2,
    )
    if func is None:

        def decorate(target: F) -> F:
            return target

        return decorate
    return func


def compose(*decorators: Callable[[Any], Any]) -> Callable[[F], F]:
    """Combine decorators into one, applied as if stacked from top to bottom.

    ``compose(a, b, c)(f)`` is the same as ``a(b(c(f)))``. With no decorators it
    returns the function unchanged.

    Args:
        *decorators: Decorators to combine, outermost first.

    Returns:
        A single decorator that applies all of them.

    Raises:
        TypeError: If any argument is not callable.

    Example:
        ```python
        @compose(deprecate("old api"), mock_data(7))
        def value() -> int:
            return 1
        ```
    """
    for index, decorator in enumerate(decorators):
        if not callable(decorator):
            raise TypeError(
                f"compose() arguments must be callable; argument {index} is {decorator!r}"
            )

    def decorate(func: F) -> F:
        result: Any = func
        for decorator in reversed(decorators):
            result = decorator(result)
        return cast(F, result)

    return decorate


C = TypeVar("C", bound=type)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def wrap_methods(
    *decorators: Callable[[Any], Any],
    include: Iterable[str] | Callable[[str], bool] | None = None,
    exclude: Iterable[str] | Callable[[str], bool] | None = None,
    include_private: bool = False,
) -> Callable[[C], C]:
    """Class decorator that applies decorators to the methods of a class.

    Decorators are given outermost first, like `compose`; a `Policy` works too.
    Plain, ``async def``, static and class methods are all handled: the underlying
    function is decorated and re-wrapped in ``staticmethod``/``classmethod``. Only
    methods defined on the class itself are touched, not inherited ones.

    By default dunder methods, names starting with ``_`` and properties are skipped.
    Naming one in `include` (a collection of names, not a predicate) opts it in;
    for a property, the getter, setter and deleter are all decorated.

    Args:
        *decorators: Decorators to apply, outermost first.
        include: Names (or a predicate on the name) to restrict the selection to.
            ``None`` selects every eligible method.
        exclude: Names (or a predicate) to leave alone. Wins over `include`.
        include_private: Also select single-underscore names such as ``_helper``.

    Returns:
        A class decorator that modifies and returns the same class.

    Raises:
        TypeError: If any decorator is not callable.

    Example:
        ```python
        @wrap_methods(retry(max_attempts=3, on=ConnectionError), exclude=["close"])
        class Client:
            def get(self, url: str) -> str: ...
            def close(self) -> None: ...
        ```
    """
    for index, decorator in enumerate(decorators):
        if not callable(decorator):
            raise TypeError(
                f"wrap_methods() arguments must be callable; argument {index} is {decorator!r}"
            )

    def matcher(spec: Iterable[str] | Callable[[str], bool]) -> Callable[[str], bool]:
        if callable(spec):
            return spec
        names = {spec} if isinstance(spec, str) else set(spec)
        return names.__contains__

    explicit: frozenset[str] = frozenset()
    if include is not None and not callable(include):
        explicit = frozenset({include} if isinstance(include, str) else include)
    include_ok = matcher(include) if include is not None else (lambda _n: True)
    exclude_ok = matcher(exclude) if exclude is not None else (lambda _n: False)

    def decorate_func(func: Any) -> Any:
        result = func
        for decorator in reversed(decorators):
            result = decorator(result)
        return result

    def decorate(cls: C) -> C:
        for name, attr in list(vars(cls).items()):
            if exclude_ok(name) or not include_ok(name):
                continue
            named = name in explicit
            if not named and (_is_dunder(name) or (name.startswith("_") and not include_private)):
                continue
            if isinstance(attr, property):
                if not named:
                    continue
                fget, fset, fdel = (
                    decorate_func(p) if p is not None else None
                    for p in (attr.fget, attr.fset, attr.fdel)
                )
                setattr(cls, name, property(fget, fset, fdel, attr.__doc__))
            elif isinstance(attr, (staticmethod, classmethod)):
                setattr(cls, name, type(attr)(decorate_func(attr.__func__)))
            elif inspect.isfunction(attr):
                setattr(cls, name, decorate_func(attr))
        return cls

    return decorate
