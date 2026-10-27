"""Argument validation and runtime type checking.

* :func:`validate_args` - run a predicate per argument (``x=lambda x: x > 0``).
* :func:`check_type` - ``isinstance`` check per argument (``x=int``).
* :func:`enforce_types` - check arguments and the return value against the function's own
  annotations.

All three work on plain functions, ``async def`` functions and methods, can be stacked, and
keep the wrapped function's signature.

Supported annotations for :func:`enforce_types`
-----------------------------------------------

Checked: plain classes (``int`` also accepts ``bool``; ``float`` accepts ``int`` and
``complex`` accepts ``int``/``float``, following PEP 484), ``Any``, ``None``,
``Optional`` / ``Union`` / ``X | Y``, ``Literal``, ``list[T]``, ``set[T]``, ``frozenset[T]``,
``tuple[T, ...]``, ``tuple[A, B]``, ``dict[K, V]`` (elements are checked),
``collections.abc`` ABCs such as ``Sequence[T]``, ``MutableSequence[T]``, ``Set[T]``,
``Mapping[K, V]`` (elements checked) and ``Iterable[T]`` / ``Iterator[T]`` / ``Generator``
(``isinstance`` only, iterators are never consumed), ``Callable`` (``callable()`` only),
``type[T]``, ``TypeVar`` (bound or constraints, otherwise anything), ``Annotated[T, ...]``
(checks ``T``), ``NewType`` (checks the supertype) and ``runtime_checkable`` protocols.

Accepted without checking: ``TypedDict``, protocols that are not ``runtime_checkable``,
forward references that cannot be resolved and any other construct this module does not
understand. A check that cannot be performed never raises.
"""

from __future__ import annotations

import collections.abc as cabc
import functools
import inspect
import reprlib
import types
import typing
from collections.abc import Callable
from typing import Any, TypeVar, Union, overload

from python_wrap import _core, errors
from python_wrap._core import P, R

__all__ = ["check_type", "enforce_types", "validate_args"]

Validator = Callable[[Any], object] | tuple[Callable[[Any], object], str]

_NoneType = type(None)
_UnionType = getattr(types, "UnionType", None)


def _repr(value: object) -> str:
    return reprlib.repr(value)


def _wrap(
    func: Callable[P, R],
    before: Callable[[tuple[Any, ...], dict[str, Any]], None],
    after: Callable[[Any], None] | None = None,
) -> Callable[P, R]:
    """Build a sync or async wrapper that runs ``before`` (and optionally ``after``)."""
    if _core.is_async(func):
        afunc: Any = func

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            before(args, kwargs)
            result = await afunc(*args, **kwargs)
            if after is not None:
                after(result)
            return result

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        before(args, kwargs)
        result = func(*args, **kwargs)
        if after is not None:
            after(result)
        return result

    return wrapper


def _check_names(
    func: Callable[..., Any], sig: inspect.Signature, names: typing.Iterable[str], who: str
) -> None:
    unknown = [n for n in names if n not in sig.parameters]
    if unknown:
        raise TypeError(
            f"{who}() got unknown parameter name(s) {', '.join(map(repr, unknown))} "
            f"for {_core.qualname(func)}{sig}"
        )


# --------------------------------------------------------------------------------------
# validate_args
# --------------------------------------------------------------------------------------


def validate_args(**validators: Validator) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Validate arguments with predicates.

    Each keyword names a parameter and gives a callable that receives the argument value
    (defaults are applied first, so default values are validated too). A falsy result raises
    :class:`~python_wrap.errors.ValidationError`, which is a ``ValueError``. A validator may
    instead raise its own exception, which propagates unchanged. Pass a
    ``(callable, "message")`` tuple for a custom error message. Validators for ``*args`` /
    ``**kwargs`` parameters receive the whole tuple / dict.

    Args:
        **validators: ``parameter_name=predicate`` or ``parameter_name=(predicate, message)``.

    Returns:
        A decorator that preserves the function's signature.

    Raises:
        TypeError: At decoration time, if a name is not a parameter of the function or a
            validator is not callable.
        python_wrap.errors.ValidationError: At call time, when a validator returns falsy.

    Example:
        >>> @validate_args(x=lambda x: x > 0, name=(lambda s: s != "", "name must not be empty"))
        ... def greet(x, name):
        ...     return name * x
        >>> greet(2, "ab")
        'abab'
    """
    normalised: dict[str, tuple[Callable[[Any], object], str | None]] = {}
    for name, spec in validators.items():
        if isinstance(spec, tuple):
            if len(spec) != 2 or not callable(spec[0]) or not isinstance(spec[1], str):
                raise TypeError(f"validator for {name!r} must be a callable or (callable, str)")
            normalised[name] = (spec[0], spec[1])
        elif callable(spec):
            normalised[name] = (spec, None)
        else:
            raise TypeError(f"validator for {name!r} must be callable, got {type(spec).__name__}")

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        _check_names(func, sig, normalised, "validate_args")

        def before(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for name, (predicate, message) in normalised.items():
                value = bound.arguments[name]
                if not predicate(value):
                    raise errors.ValidationError(
                        message or f"parameter {name!r} failed validation (got {_repr(value)})",
                        parameter=name,
                        value=value,
                    )

        return _wrap(func, before)

    return decorate


# --------------------------------------------------------------------------------------
# check_type
# --------------------------------------------------------------------------------------


def _type_name(tp: object) -> str:
    if isinstance(tp, tuple):
        return " | ".join(_type_name(t) for t in tp)
    if isinstance(tp, type):
        return tp.__name__
    return str(tp).replace("typing.", "")


def check_type(**type_hints: type | tuple[type, ...]) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Check argument types with ``isinstance``.

    Each keyword names a parameter and gives a class (or a tuple of classes). Defaults are
    not checked, only the arguments that are actually passed. For ``*args`` / ``**kwargs``
    parameters the type applies to every element / value.

    Args:
        **type_hints: ``parameter_name=type`` (anything ``isinstance`` accepts).

    Returns:
        A decorator that preserves the function's signature.

    Raises:
        TypeError: At decoration time, if a name is not a parameter of the function.
        python_wrap.errors.ArgumentTypeError: At call time, on a mismatch (a ``TypeError``).

    Example:
        >>> @check_type(x=int, y=str)
        ... def f(x, y):
        ...     return y * x
        >>> f(2, "a")
        'aa'
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        _check_names(func, sig, type_hints, "check_type")

        def check(label: str, value: Any, expected: Any) -> None:
            if not isinstance(value, expected):
                raise errors.ArgumentTypeError(
                    f"parameter {label!r} expected {_type_name(expected)}, "
                    f"got {type(value).__name__}",
                    parameter=label,
                    expected=expected,
                    value=value,
                )

        def before(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            bound = sig.bind(*args, **kwargs)
            for name, expected in type_hints.items():
                if name not in bound.arguments:
                    continue
                value = bound.arguments[name]
                kind = sig.parameters[name].kind
                if kind is inspect.Parameter.VAR_POSITIONAL:
                    for i, item in enumerate(value):
                        check(f"{name}[{i}]", item, expected)
                elif kind is inspect.Parameter.VAR_KEYWORD:
                    for key, item in value.items():
                        check(f"{name}[{key!r}]", item, expected)
                else:
                    check(name, value, expected)

        return _wrap(func, before)

    return decorate


# --------------------------------------------------------------------------------------
# enforce_types
# --------------------------------------------------------------------------------------

_ELEMENT_CHECKED_ABCS: tuple[type, ...] = (
    cabc.Sequence,
    cabc.MutableSequence,
    cabc.Set,
    cabc.MutableSet,
)
_MAPPING_ABCS: tuple[type, ...] = (cabc.Mapping, cabc.MutableMapping)


def _fmt(hint: object) -> str:
    """Readable name for an annotation, e.g. ``list[int]`` or ``int | None``."""
    if hint is None or hint is _NoneType:
        return "None"
    if isinstance(hint, type) and typing.get_origin(hint) is None:
        return hint.__name__
    if isinstance(hint, typing.ForwardRef):
        return hint.__forward_arg__
    text = repr(hint)
    for prefix in ("typing.", "collections.abc."):
        text = text.replace(prefix, "")
    return text


def _got(value: object) -> str:
    return type(value).__name__


def _nested(outer: str, inner: str, where: str, element: object) -> str:
    if " " not in inner:
        return f"{outer} containing {inner} {where}"
    return f"{outer} containing {_got(element)} {where} ({inner})"


def _isinstance(value: object, cls: Any) -> bool:
    try:
        return isinstance(value, cls)
    except TypeError:
        return True  # cannot be checked at runtime, so accept


def _check_items(value: Any, hint_args: tuple[Any, ...]) -> str | None:
    """Element checks for list/set/frozenset/Sequence/Set (``origin`` already matched)."""
    if len(hint_args) != 1:
        return None
    (item_hint,) = hint_args
    if item_hint is Any:
        return None
    is_seq = isinstance(value, cabc.Sequence)
    for i, item in enumerate(value):
        problem = _mismatch(item, item_hint)
        if problem is not None:
            where = f"at index {i}" if is_seq else ""
            return _nested(_got(value), problem, where, item).rstrip()
    return None


def _check_mapping(value: Any, hint_args: tuple[Any, ...]) -> str | None:
    if len(hint_args) != 2:
        return None
    key_hint, value_hint = hint_args
    for key, item in value.items():
        problem = _mismatch(key, key_hint)
        if problem is not None:
            return (
                f"{_got(value)} with key {problem}"
                if " " not in problem
                else (f"{_got(value)} with {_got(key)} key {_repr(key)} ({problem})")
            )
        problem = _mismatch(item, value_hint)
        if problem is not None:
            where = f"at key {_repr(key)}"
            if " " not in problem:
                return f"{_got(value)} with {problem} value {where}"
            return f"{_got(value)} with {_got(item)} value {where} ({problem})"
    return None


def _check_tuple(value: Any, hint_args: tuple[Any, ...]) -> str | None:
    if len(hint_args) == 2 and hint_args[1] is Ellipsis:
        for i, item in enumerate(value):
            problem = _mismatch(item, hint_args[0])
            if problem is not None:
                return _nested("tuple", problem, f"at index {i}", item)
        return None
    if hint_args == ((),):
        hint_args = ()
    if len(value) != len(hint_args):
        return f"tuple of length {len(value)}"
    for i, (item, item_hint) in enumerate(zip(value, hint_args, strict=True)):
        problem = _mismatch(item, item_hint)
        if problem is not None:
            if " " not in problem:
                return f"tuple with {problem} at index {i}"
            return f"tuple with {_got(item)} at index {i} ({problem})"
    return None


def _mismatch(value: Any, hint: Any) -> str | None:
    """Return a description of what ``value`` is if it violates ``hint``, else ``None``."""
    if hint is Any or hint is object or isinstance(hint, (str, typing.ForwardRef)):
        return None
    if hint is None or hint is _NoneType:
        return None if value is None else _got(value)
    if isinstance(hint, TypeVar):
        if hint.__bound__ is not None:
            return _mismatch(value, hint.__bound__)
        if hint.__constraints__:
            if any(_mismatch(value, c) is None for c in hint.__constraints__):
                return None
            return _got(value)
        return None
    supertype = getattr(hint, "__supertype__", None)
    if supertype is not None:  # NewType
        return _mismatch(value, supertype)

    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin is typing.Annotated:
        return _mismatch(value, args[0])
    if origin is Union or (_UnionType is not None and origin is _UnionType):
        members = [a for a in args if a is not _NoneType]
        if any(_mismatch(value, a) is None for a in args):
            return None
        if value is not None and len(members) == 1:
            return _mismatch(value, members[0])
        return _got(value)
    if origin is typing.Literal:
        for lit in args:
            if type(value) is type(lit) and value == lit:
                return None
        return _got(value) if not isinstance(value, (str, int, bytes, bool)) else _repr(value)
    if origin is None:
        if typing.is_typeddict(hint):
            return None
        if hint is typing.Callable or hint is cabc.Callable:
            return None if callable(value) else _got(value)
        if isinstance(hint, type):
            if getattr(hint, "_is_protocol", False):
                if not getattr(hint, "_is_runtime_protocol", False):
                    return None
                return None if _isinstance(value, hint) else _got(value)
            if hint is float and isinstance(value, int) and not isinstance(value, bool):
                return None
            if hint is float and isinstance(value, bool):
                return None
            if hint is complex and isinstance(value, (int, float)):
                return None
            return None if _isinstance(value, hint) else _got(value)
        return None

    # Parameterised generics from here on.
    if origin is cabc.Callable:
        return None if callable(value) else _got(value)
    if origin is type:
        if not isinstance(value, type):
            return _got(value)
        if not args:
            return None
        target = args[0]
        if target is Any or isinstance(target, TypeVar):
            return None
        is_union = typing.get_origin(target) in (Union, _UnionType)
        targets = typing.get_args(target) if is_union else (target,)
        for t in targets:
            # On 3.10 ``isinstance(list[int], type)`` is True, so rule out generic aliases too.
            if not isinstance(t, type) or typing.get_origin(t) is not None:
                return None
            if issubclass(value, t):
                return None
        return f"type {value.__name__}"
    if not isinstance(origin, type):
        return None
    if getattr(origin, "_is_protocol", False):
        return None
    if not _isinstance(value, origin):
        return _got(value)

    if origin in (list, set, frozenset) or origin in _ELEMENT_CHECKED_ABCS:
        return _check_items(value, args)
    if origin is dict or origin in _MAPPING_ABCS:
        return _check_mapping(value, args)
    if origin is tuple:
        return _check_tuple(value, args)
    return None  # Iterable/Iterator/other generics: isinstance only


class _Checker:
    """Lazily resolves annotations on first call and checks calls against them."""

    def __init__(self, func: Callable[..., Any], check_return: bool) -> None:
        self.func = func
        self.check_return = check_return
        self.sig = inspect.signature(func)
        self.hints: dict[str, Any] | None = None

    def _resolve(self) -> dict[str, Any]:
        if self.hints is not None:
            return self.hints
        try:
            hints = typing.get_type_hints(self.func)
        except Exception:
            # Unresolvable forward reference (so far): fall back to raw annotations, where
            # strings are accepted, and try again on the next call.
            return dict(getattr(self.func, "__annotations__", {}))
        self.hints = hints
        return hints

    @staticmethod
    def _fail(label: str, hint: Any, value: Any, problem: str, what: str) -> typing.NoReturn:
        raise errors.ArgumentTypeError(
            f"{what} {label!r} expected {_fmt(hint)}, got {problem}"
            if what == "parameter"
            else f"{what} expected {_fmt(hint)}, got {problem}",
            parameter=label,
            expected=hint,
            value=value,
        )

    def _check_one(self, label: str, value: Any, hint: Any) -> None:
        problem = _mismatch(value, hint)
        if problem is not None:
            self._fail(label, hint, value, problem, "parameter")

    def before(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        hints = self._resolve()
        if not hints:
            return
        bound = self.sig.bind(*args, **kwargs)
        for name, value in bound.arguments.items():
            if name not in hints:
                continue
            hint = hints[name]
            kind = self.sig.parameters[name].kind
            if kind is inspect.Parameter.VAR_POSITIONAL:
                for i, item in enumerate(value):
                    self._check_one(f"{name}[{i}]", item, hint)
            elif kind is inspect.Parameter.VAR_KEYWORD:
                for key, item in value.items():
                    self._check_one(f"{name}[{key!r}]", item, hint)
            else:
                self._check_one(name, value, hint)

    def after(self, result: Any) -> None:
        hints = self._resolve()
        if "return" not in hints:
            return
        hint = hints["return"]
        problem = _mismatch(result, hint)
        if problem is not None:
            self._fail("return", hint, result, problem, "return value")


@overload
def enforce_types(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def enforce_types(
    func: None = None, /, *, check_return: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def enforce_types(
    func: Callable[P, R] | None = None, /, *, check_return: bool = True
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Check arguments and the return value against the function's own annotations.

    Annotations are resolved lazily on the first call with :func:`typing.get_type_hints`, so
    forward references work. Only arguments that are actually passed are checked (defaults
    are not). For ``async def`` functions the awaited result is checked. See the module
    documentation for the supported subset of annotations; anything unsupported is accepted
    rather than rejected.

    Args:
        func: The function (when used as a bare ``@enforce_types``).
        check_return: Also check the return value against the return annotation.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        python_wrap.errors.ArgumentTypeError: On a mismatch (a ``TypeError``), with a message
            such as ``parameter 'items' expected list[int], got list containing str at
            index 2``. ``error.parameter`` is ``"return"`` for the return value.

    Example:
        >>> @enforce_types
        ... def total(items: list[int]) -> int:
        ...     return sum(items)
        >>> total([1, 2])
        3
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        checker = _Checker(f, check_return)
        return _wrap(f, checker.before, checker.after if check_return else None)

    return decorate if func is None else decorate(func)
