"""enforce_types: TypedDict checks, parameterised runtime protocols and strict=True."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    NewType,
    NoReturn,
    Optional,
    Protocol,
    SupportsAbs,
    TypedDict,
    TypeVar,
    Union,
    runtime_checkable,
)

import pytest

from python_wrap import errors
from python_wrap.validation import UnsupportedAnnotationError, _unsupported, enforce_types

T = TypeVar("T")


class Movie(TypedDict):
    title: str
    year: int


class Partial(TypedDict, total=False):
    rating: float


class Nested(TypedDict):
    movie: Movie
    tags: list[str]


class Tree(TypedDict):
    name: str
    children: list[Tree]


@runtime_checkable
class Closer(Protocol):
    def close(self) -> None: ...


class Plain(Protocol):
    def go(self) -> None: ...


class Box(Generic[T]):
    pass


UserId = NewType("UserId", int)


def message(func: Callable[..., Any], *args: Any) -> str:
    with pytest.raises(errors.ArgumentTypeError) as info:
        func(*args)
    return str(info.value)


# ----------------------------------------------------------------------- TypedDict


def test_typeddict_required_keys_and_value_types() -> None:
    @enforce_types
    def show(movie: Movie) -> str:
        return movie["title"]

    assert show({"title": "Up", "year": 2009}) == "Up"
    assert show({"title": "Up", "year": 2009, "extra": True}) == "Up"
    assert "got dict missing key 'year'" in message(show, {"title": "Up"})
    assert "got dict with str value at key 'year'" in message(show, {"title": "Up", "year": "2009"})
    assert "expected Movie, got list" in message(show, [])


def test_typeddict_optional_nested_and_recursive() -> None:
    @enforce_types
    def rate(p: Partial, n: Nested, t: Tree) -> None:
        return None

    tree: Any = {"name": "root", "children": [{"name": "leaf", "children": []}]}
    rate({}, {"movie": {"title": "A", "year": 1}, "tags": ["x"]}, tree)
    assert "key 'rating'" in message(
        rate, {"rating": "high"}, {"movie": {"title": "A", "year": 1}, "tags": []}, tree
    )
    problem = message(rate, {}, {"movie": {"title": "A"}, "tags": []}, tree)
    assert "at key 'movie'" in problem and "missing key 'year'" in problem
    bad_tree: Any = {"name": "root", "children": [{"name": 1, "children": []}]}
    assert "at key 'children'" in message(
        rate, {}, {"movie": {"title": "A", "year": 1}, "tags": []}, bad_tree
    )


def test_typeddict_with_unresolvable_annotation_is_accepted_by_default() -> None:
    ghost = TypedDict("ghost", {"x": "Missing"})  # type: ignore[misc]  # noqa: UP013, F821

    def f(g: Any) -> None:
        return None

    f.__annotations__ = {"g": ghost}
    checked = enforce_types(f)
    checked({"anything": 1})
    with pytest.raises(UnsupportedAnnotationError, match="could not be resolved"):
        enforce_types(strict=True)(f)


# --------------------------------------------------------- parameterised protocols


def test_parameterised_runtime_protocol_is_checked() -> None:
    @enforce_types
    def magnitude(x: SupportsAbs[int]) -> int:
        return int(abs(x))

    assert magnitude(-3) == 3
    assert "got str" in message(magnitude, "x")


# --------------------------------------------------------------------- strict=True


def test_strict_accepts_fully_checkable_signatures() -> None:
    @enforce_types(strict=True)
    def ok(
        a: int,
        b: Optional[str],  # noqa: UP045
        c: list[dict[str, tuple[int, ...]]],
        d: Literal["x", "y"],
        e: Annotated[float, "meta"],
        f: Closer,
        g: Movie,
        h: Callable[[int], str],
        i: type[int],
        j: Iterable[int],
        k: Box[int],
        m: UserId,
        n: Union[int, None],  # noqa: UP007
        o: Sequence[Any],
        p: T,
        q: Tree,
        r: SupportsAbs[int],
    ) -> None:
        return None

    assert callable(ok)

    @enforce_types(strict=True)
    def never() -> NoReturn:
        raise RuntimeError

    with pytest.raises(RuntimeError):
        never()


@pytest.mark.parametrize(
    ("annotation", "reason"),
    [
        (Plain, "not @runtime_checkable"),
        (list[Plain], "not @runtime_checkable"),
        (Optional[Plain], "not @runtime_checkable"),  # noqa: UP045
        (ClassVar[int], "cannot be checked at runtime"),
        (TypeVar("B", bound=Plain), "not @runtime_checkable"),
        (NewType("P", Plain), "not @runtime_checkable"),  # type: ignore[valid-newtype]
        ("NotDefined", "could not be resolved"),
    ],
)
def test_unsupported_reasons(annotation: Any, reason: str) -> None:
    assert reason in (_unsupported(annotation) or "")


def test_unsupported_generic_protocol_and_typeddict_fields() -> None:
    class GenericPlain(Protocol[T]):
        def get(self) -> T: ...

    class Holder(TypedDict):
        item: Plain

    assert "not @runtime_checkable" in (_unsupported(GenericPlain[int]) or "")
    assert "key 'item' of" in (_unsupported(Holder) or "")
    assert _unsupported(Annotated[Plain, "x"]) is not None
    assert _unsupported(dict[str, Plain]) is not None
    assert _unsupported(tuple[int, ...]) is None
    assert _unsupported(tuple[()]) is None


@pytest.mark.skipif(sys.version_info < (3, 11), reason="typing.Self is 3.11+")
def test_strict_rejects_self() -> None:
    from typing import Self

    assert "cannot be checked" in (_unsupported(Self) or "")


def test_strict_raises_at_decoration_with_details() -> None:
    def f(p: Plain) -> int:
        return 1

    with pytest.raises(UnsupportedAnnotationError) as info:
        enforce_types(strict=True)(f)
    error = info.value
    assert isinstance(error, TypeError) and isinstance(error, errors.PythonWrapError)
    assert error.parameter == "p" and error.annotation is Plain
    assert "parameter 'p'" in str(error) and "Plain" in str(error)
    enforce_types(f)(object())  # default: accepted silently


def test_strict_return_annotation_and_check_return_false() -> None:
    def f() -> Plain:
        return object()  # type: ignore[return-value]

    with pytest.raises(UnsupportedAnnotationError, match="return annotation"):
        enforce_types(strict=True)(f)
    assert enforce_types(strict=True, check_return=False)(f)() is not None


def test_strict_forward_reference_defers_to_first_call() -> None:
    @enforce_types(strict=True)
    def f(x: LaterDefined) -> int:  # type: ignore[name-defined]  # noqa: F821
        return 1

    with pytest.raises(UnsupportedAnnotationError, match="could not be resolved"):
        f(1)

    @enforce_types(strict=True)
    def g(x: DefinedSoon) -> int:
        return 1

    assert g(DefinedSoon()) == 1
    with pytest.raises(errors.ArgumentTypeError):
        g(1)


class DefinedSoon:
    pass


def test_strict_async_function() -> None:
    with pytest.raises(UnsupportedAnnotationError):

        @enforce_types(strict=True)
        async def bad(p: Plain) -> None:
            return None

    @enforce_types(strict=True)
    async def good(m: Movie) -> str:
        return m["title"]

    assert asyncio.run(good({"title": "A", "year": 1})) == "A"
