import collections.abc as cabc
import inspect
import typing
from collections import deque
from collections.abc import Callable
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    Protocol,
    TypedDict,
    TypeVar,
    Union,
    runtime_checkable,
)

import pytest
from hypothesis import given
from hypothesis import strategies as st

from python_wrap import errors
from python_wrap.validation import check_type, enforce_types, validate_args

_X = TypeVar("_X")


class Box(typing.Generic[_X]):
    pass


# --------------------------------------------------------------------------- validate_args


def test_validate_args_passes_and_fails() -> None:
    @validate_args(x=lambda x: x > 0)
    def f(x: int) -> int:
        return x

    assert f(3) == 3
    with pytest.raises(errors.ValidationError) as exc:
        f(-1)
    assert exc.value.parameter == "x"
    assert exc.value.value == -1
    assert isinstance(exc.value, ValueError)


def test_validate_args_kwargs_defaults_and_custom_message() -> None:
    @validate_args(x=(lambda x: x > 0, "x must be positive"), y=lambda y: y < 10)
    def f(x: int, y: int = 50) -> int:
        return x + y

    with pytest.raises(errors.ValidationError, match="x must be positive"):
        f(x=0, y=1)
    with pytest.raises(errors.ValidationError, match="'y'"):
        f(1)  # default is validated


def test_validate_args_own_exception_propagates() -> None:
    def check(v: int) -> bool:
        raise KeyError("boom")

    @validate_args(x=check)
    def f(x: int) -> int:
        return x

    with pytest.raises(KeyError):
        f(1)


def test_validate_args_unknown_name_and_bad_validator() -> None:
    with pytest.raises(TypeError, match="'z'"):

        @validate_args(z=lambda z: True)
        def f(x: int) -> int:
            return x

    with pytest.raises(TypeError):
        validate_args(x=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_args(x=(lambda x: True, 3))  # type: ignore[arg-type]


def test_validate_args_var_params_get_whole_collection() -> None:
    @validate_args(args=lambda a: len(a) == 2, kwargs=lambda k: "k" in k)
    def f(*args: int, **kwargs: int) -> int:
        return len(args)

    assert f(1, 2, k=1) == 2
    with pytest.raises(errors.ValidationError):
        f(1, k=1)
    with pytest.raises(errors.ValidationError):
        f(1, 2)


def test_validate_args_bad_call_raises_typeerror() -> None:
    @validate_args(x=lambda x: True)
    def f(x: int) -> int:
        return x

    with pytest.raises(TypeError):
        f()  # type: ignore[call-arg]


async def test_validate_args_async() -> None:
    @validate_args(x=lambda x: x > 0)
    async def f(x: int) -> int:
        return x

    assert inspect.iscoroutinefunction(f)
    assert await f(1) == 1
    with pytest.raises(errors.ValidationError):
        await f(0)


def test_validate_args_method() -> None:
    class C:
        @validate_args(x=lambda x: x > 0)
        def m(self, x: int) -> int:
            return x

        @classmethod
        @validate_args(x=lambda x: x > 0)
        def c(cls, x: int) -> int:
            return x

        @staticmethod
        @validate_args(x=lambda x: x > 0)
        def s(x: int) -> int:
            return x

    assert C().m(1) == C.c(1) == C.s(1) == 1
    for call in (C().m, C.c, C.s):
        with pytest.raises(ValueError):
            call(-1)


# --------------------------------------------------------------------------- check_type


def test_check_type_basic() -> None:
    @check_type(x=int, y=(str, bytes))
    def f(x: Any, y: Any = "a") -> Any:
        return x

    assert f(1) == 1
    assert f(1, b"x") == 1
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'x' expected int, got str") as e:
        f("no")
    assert isinstance(e.value, TypeError)
    assert e.value.parameter == "x"
    with pytest.raises(TypeError, match="str \\| bytes"):
        f(1, 5)


def test_check_type_unknown_name() -> None:
    with pytest.raises(TypeError):

        @check_type(nope=int)
        def f(x: int) -> int:
            return x


def test_check_type_var_params() -> None:
    @check_type(args=int, kwargs=str)
    def f(*args: Any, **kwargs: Any) -> None:
        return None

    f(1, 2, a="x")
    with pytest.raises(errors.ArgumentTypeError, match=r"args\[1\]"):
        f(1, "x")
    with pytest.raises(errors.ArgumentTypeError, match=r"kwargs\['a'\]"):
        f(a=1)


async def test_check_type_async_and_kwargs() -> None:
    @check_type(x=int)
    async def f(x: Any) -> Any:
        return x

    assert await f(x=2) == 2
    with pytest.raises(TypeError):
        await f(x="a")


def test_check_type_skips_missing_arg() -> None:
    @check_type(y=int)
    def f(x: int, y: Any = None) -> int:
        return x

    assert f(1) == 1  # y not passed: defaults are not checked


# --------------------------------------------------------------------------- enforce_types


def fails(func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    with pytest.raises(errors.ArgumentTypeError) as exc:
        func(*args, **kwargs)
    return str(exc.value)


@enforce_types
def _list_int(items: list[int]) -> int:
    return sum(items)


def test_precise_message() -> None:
    assert fails(_list_int, [1, 2, "x"]) == (
        "parameter 'items' expected list[int], got list containing str at index 2"
    )
    assert fails(_list_int, (1,)) == "parameter 'items' expected list[int], got tuple"
    assert _list_int([1, 2]) == 3


def test_bare_and_parameterised_forms() -> None:
    @enforce_types()
    def a(x: int) -> int:
        return x

    @enforce_types(check_return=False)
    def b(x: int) -> int:
        return "bad"  # type: ignore[return-value]

    assert a(1) == 1
    assert b(1) == "bad"
    assert "parameter 'x'" in fails(a, "s")


def test_return_checked() -> None:
    @enforce_types
    def f(x: int) -> str:
        return x  # type: ignore[return-value]

    msg = fails(f, 1)
    assert msg == "return value expected str, got int"


def test_none_return_and_defaults_not_checked() -> None:
    @enforce_types
    def f(x: int = None) -> None:  # type: ignore[assignment]  # noqa: RUF013
        return None

    f()
    assert "got str" in fails(f, "s")

    @enforce_types
    def g() -> None:
        return 1  # type: ignore[return-value]

    assert "expected None" in fails(g)


def test_no_annotations_is_noop() -> None:
    @enforce_types
    def f(x, y=1):  # type: ignore[no-untyped-def]
        return x

    assert f("anything") == "anything"


def test_plain_classes_and_numeric_tower() -> None:
    @enforce_types
    def f(a: float, b: complex, c: int) -> None:
        return None

    f(1, 2.5, True)
    f(True, 1, 1)
    assert "expected int, got float" in fails(f, 1.0, 1, 1.5)
    assert "expected float, got str" in fails(f, "x", 1, 1)
    assert "expected complex" in fails(f, 1.0, "x", 1)


def test_any_object_and_typevar() -> None:
    T = TypeVar("T")
    B = TypeVar("B", bound=int)
    C = TypeVar("C", int, str)

    @enforce_types
    def f(a: Any, b: object, c: T, d: B, e: C) -> None:
        return None

    f(1, 2, 3, 4, "x")
    assert "parameter 'd'" in fails(f, 1, 2, 3, "no", 1)
    assert "got float" in fails(f, 1, 2, 3, 4, 1.5)


def test_optional_union_pipe() -> None:
    @enforce_types
    def f(a: int | None, b: Union[int, str], c: int | None = None, d: list[int] | None = None):  # type: ignore[no-untyped-def]  # noqa: UP007
        return None

    f(None, "x")
    f(1, 2, None, [1])
    assert "parameter 'a' expected" in fails(f, "x", 1)
    assert "got float" in fails(f, 1, 1.5)
    assert "list containing str at index 0" in fails(f, 1, 1, None, ["x"])


def test_literal() -> None:
    @enforce_types
    def f(mode: Literal["r", "w"], n: Literal[1] = 1) -> None:
        return None

    f("r")
    assert "expected Literal['r', 'w'], got 'x'" in fails(f, "x")
    assert "got True" in fails(f, "r", True)


def test_containers() -> None:
    @enforce_types
    def f(
        a: set[int] = frozenset(),  # type: ignore[assignment]
        b: frozenset[str] = frozenset(),
        c: tuple[int, ...] = (),
        d: tuple[int, str] = (1, "a"),
        e: dict[str, int] | None = None,
        g: tuple[()] = (),
    ) -> None:
        return None

    f({1}, frozenset("a"), (1, 2), (1, "x"), {"a": 1}, ())
    assert "set containing str" in fails(f, a={"x"})
    assert "frozenset containing int" in fails(f, b=frozenset({1}))
    assert fails(f, c=(1, "x")) == (
        "parameter 'c' expected tuple[int, ...], got tuple containing str at index 1"
    )
    assert "tuple of length 3" in fails(f, d=(1, "a", 3))
    assert "tuple with int at index 1" in fails(f, d=(1, 2))
    assert "dict with str value at key 'a'" in fails(f, e={"a": "s"})
    assert "dict with key int" in fails(f, e={1: 1})
    assert "tuple of length 1" in fails(f, g=(1,))


def test_nested_messages() -> None:
    @enforce_types
    def f(
        x: list[list[int]] = [],  # noqa: B006
        y: dict[str, list[int]] = {},  # noqa: B006
        z: tuple[list[int], str] = ([], ""),
        k: dict[tuple[int, int], int] = {},  # noqa: B006
    ) -> None:
        return None

    assert fails(f, x=[[1], ["a"]]) == (
        "parameter 'x' expected list[list[int]], got "
        "list containing list at index 1 (list containing str at index 0)"
    )
    assert "dict with list value at key 'a' (list containing str at index 0)" in fails(
        f, y={"a": ["s"]}
    )
    assert "tuple with list at index 0 (list containing str at index 0)" in fails(f, z=(["s"], ""))
    assert "dict with tuple key (1, 'a')" in fails(f, k={(1, "a"): 1})


def test_abc_generics() -> None:
    @enforce_types
    def f(
        s: cabc.Sequence[int] = (),
        m: cabc.Mapping[str, int] = {},
        i: cabc.Iterable[int] = (),
        it: cabc.Iterator[int] | None = None,
        cl: cabc.Callable[[int], int] | None = None,
        st_: cabc.Set[int] = frozenset(),
    ) -> None:
        return None

    f([1], {"a": 1}, iter(["not checked"]), iter([]), len, {1})
    assert "list containing str at index 0" in fails(f, s=["a"])
    assert "dict with str value" in fails(f, m={"a": "b"})
    assert "got int" in fails(f, i=5)
    assert "got list" in fails(f, it=[1])
    assert "got int" in fails(f, cl=5)
    assert "set containing str" in fails(f, st_={"x"})


def test_iterators_not_consumed() -> None:
    @enforce_types
    def f(it: cabc.Iterable[int]) -> list[int]:
        return list(it)

    assert f(iter([1, 2])) == [1, 2]
    assert f(x for x in [3]) == [3]


def test_typing_callable_and_type() -> None:
    @enforce_types
    def f(cb: typing.Callable[..., int], bare: Callable, t: type[int] = int) -> None:  # type: ignore[type-arg]
        return None

    f(len, print)
    assert "got str" in fails(f, "x", print)
    assert "got int" in fails(f, len, 5)
    f(len, print, bool)
    assert "type str" in fails(f, len, print, str)
    assert "got int" in fails(f, len, print, 5)


def test_type_variants() -> None:
    T = TypeVar("T")

    @enforce_types
    def f(
        a: type[Any] = int,
        b: type[T] = int,  # type: ignore[valid-type]
        c: type[int | str] = int,
        d: type = int,
    ) -> None:
        return None

    f(float, float, str, float)
    assert "type float" in fails(f, c=float)
    assert "got int" in fails(f, d=1)


def test_annotated_newtype() -> None:
    UserId = NewType("UserId", int)

    @enforce_types
    def f(a: Annotated[int, "meta"], b: UserId, c: list[Annotated[int, "m"]] = []) -> None:  # noqa: B006
        return None

    f(1, UserId(2), [1])
    assert "expected int, got str" in fails(f, "x", 1)
    assert "got str" in fails(f, 1, "x")
    assert "list containing str" in fails(f, 1, 1, ["x"])


def test_protocols_and_typeddict_accepted() -> None:
    @runtime_checkable
    class Closer(Protocol):
        def close(self) -> None: ...

    class Plain(Protocol):
        def go(self) -> None: ...

    class TD(TypedDict):
        a: int

    class Res:
        def close(self) -> None: ...

    @enforce_types
    def f(c: Closer, p: Plain, td: TD) -> None:
        return None

    f(Res(), object(), {"a": 1})
    f(Res(), 1, {"wrong": "shape"})  # unsupported constructs are accepted
    assert "expected Closer, got int" in fails(f, 1, 1, {})


def test_user_generic_origin_isinstance_only() -> None:
    @enforce_types
    def f(b: Box[int], d: deque[int]) -> None:
        return None

    f(Box(), deque(["x"]))
    assert "got int" in fails(f, 1, deque())


def test_forward_references_resolved_lazily() -> None:
    @enforce_types
    def f(x: "Later") -> "Later":
        return x

    global Later
    Later = type("Later", (), {})  # type: ignore[misc,assignment]
    try:
        f(Later())
        assert "expected Later" in fails(f, 1)
    finally:
        del Later


def test_unresolvable_forward_ref_is_accepted() -> None:
    @enforce_types
    def f(x: "DoesNotExist", y: int) -> None:  # noqa: F821
        return None

    f("anything", 1)
    assert "parameter 'y'" in fails(f, "anything", "no")


def test_string_annotation_with_class_defined_inside() -> None:
    @enforce_types
    def f(x: "list[int]") -> None:
        return None

    f([1])
    assert "list containing str" in fails(f, ["a"])


async def test_async_enforce() -> None:
    @enforce_types
    async def f(x: int) -> str:
        return str(x) if x >= 0 else x  # type: ignore[return-value]

    assert inspect.iscoroutinefunction(f)
    assert await f(1) == "1"
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'x'"):
        await f("a")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match="return value expected str, got int"):
        await f(-1)


def test_methods() -> None:
    class C:
        @enforce_types
        def m(self, x: int) -> int:
            return x

        @classmethod
        @enforce_types
        def c(cls, x: int) -> int:
            return x

        @staticmethod
        @enforce_types
        def s(x: int) -> int:
            return x

    assert C().m(1) == C.c(1) == C.s(1) == 1
    for call in (C().m, C.c, C.s):
        with pytest.raises(TypeError):
            call("x")


def test_var_params() -> None:
    @enforce_types
    def f(*args: int, **kwargs: str) -> None:
        return None

    f(1, 2, a="x")
    assert "parameter 'args[1]'" in fails(f, 1, "x")
    assert "parameter \"kwargs['a']\"" in fails(f, a=1)


# --------------------------------------------------------------------------- shared


def test_signature_and_metadata_preserved() -> None:
    def orig(a: int, b: str = "x", *, c: float = 1.0) -> str:
        """doc"""
        return b

    for deco in (
        enforce_types,
        validate_args(a=lambda a: True),
        check_type(a=int),
    ):
        wrapped = deco(orig)
        assert inspect.signature(wrapped) == inspect.signature(orig)
        assert wrapped.__name__ == "orig"
        assert wrapped.__doc__ == "doc"


def test_stacking() -> None:
    @enforce_types
    @check_type(x=int)
    @validate_args(x=lambda x: x > 0, y=lambda y: y != "")
    def f(x: int, y: str = "a") -> str:
        return y * x

    assert f(2) == "aa"
    with pytest.raises(errors.ValidationError):
        f(0)
    with pytest.raises(errors.ArgumentTypeError):
        f("s")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'y'"):
        f(1, 5)  # type: ignore[arg-type]
    assert list(inspect.signature(f).parameters) == ["x", "y"]


async def test_stacking_async() -> None:
    @enforce_types
    @validate_args(x=lambda x: x > 0)
    async def f(x: int) -> int:
        return x

    assert inspect.iscoroutinefunction(f)
    assert await f(1) == 1
    with pytest.raises(errors.ValidationError):
        await f(-1)


# --------------------------------------------------------------------------- hypothesis

nested = st.recursive(
    st.integers(),
    lambda inner: st.lists(inner, max_size=3),
    max_leaves=10,
)


@given(st.lists(st.integers()), st.dictionaries(st.text(), st.integers()))
def test_valid_containers_always_pass(items: list[int], mapping: dict[str, int]) -> None:
    @enforce_types
    def f(a: list[int], b: dict[str, int]) -> tuple[list[int], dict[str, int]]:
        return a, b

    assert f(items, mapping) == (items, mapping)


@given(
    st.lists(st.integers(), min_size=1),
    st.data(),
    st.sampled_from(["x", 1.5, None, b"b"]),
)
def test_single_bad_element_always_fails(items: list[int], data: st.DataObject, bad: Any) -> None:
    index = data.draw(st.integers(0, len(items) - 1))
    broken: list[Any] = [*items]
    broken[index] = bad

    @enforce_types
    def f(a: list[int]) -> None:
        return None

    with pytest.raises(errors.ArgumentTypeError) as exc:
        f(broken)
    assert f"at index {index}" in str(exc.value)

    @enforce_types
    def g(a: dict[str, int]) -> None:
        return None

    mapping = {str(i): v for i, v in enumerate(items)}
    mapping[str(index)] = bad
    with pytest.raises(errors.ArgumentTypeError):
        g(mapping)


@given(nested)
def test_nested_lists_property(value: Any) -> None:
    def depth(v: Any) -> int:
        return 0 if isinstance(v, int) else 1 + max((depth(i) for i in v), default=0)

    @enforce_types
    def f(a: Any) -> None:
        return None

    f(value)
    if isinstance(value, list):

        @enforce_types
        def g(a: list[Any]) -> None:
            return None

        g(value)


def test_uncheckable_constructs_are_accepted() -> None:
    class Meta(type):
        def __instancecheck__(cls, instance: object) -> bool:
            raise TypeError("nope")

    class Weird(metaclass=Meta):
        pass

    @enforce_types
    def f(
        a: Weird,
        b: type[list[int]],
        c: typing.Final[int] = 1,  # type: ignore[valid-type]
        d: typing.ClassVar[int] = 1,  # type: ignore[valid-type]
    ) -> None:
        return None

    f(1, list)
    f(1, dict)  # type[list[int]] is not a plain class target, so it is accepted


def test_format_of_forward_ref_and_special_forms() -> None:
    from python_wrap.validation import _fmt

    assert _fmt(typing.ForwardRef("Foo")) == "Foo"
    assert _fmt(None) == "None"
    assert _fmt(typing.Optional[typing.Sequence[int]]).startswith(("Optional", "Sequence"))  # noqa: UP045


# ------------------------------------------------------------ fast paths (positional calls)


def test_validate_args_fast_path_defaults_and_kwargs() -> None:
    seen: list[tuple[str, object]] = []

    def spy(name: str) -> Callable[[Any], bool]:
        def check(v: Any) -> bool:
            seen.append((name, v))
            return True

        return check

    @validate_args(a=spy("a"), b=spy("b"), c=spy("c"))
    def f(a: int, b: int = 5, c: int = 6) -> int:
        return a + b + c

    assert f(1) == 12
    assert seen == [("a", 1), ("b", 5), ("c", 6)]  # defaults are validated
    seen.clear()
    assert f(1, 2) == 9
    assert seen == [("a", 1), ("b", 2), ("c", 6)]
    seen.clear()
    assert f(1, c=3) == 9  # keyword call takes the bind path, same result
    assert seen == [("a", 1), ("b", 5), ("c", 3)]
    seen.clear()
    assert f(a=1, b=2, c=3) == 6
    assert seen == [("a", 1), ("b", 2), ("c", 3)]


def test_validate_args_fast_path_errors_match() -> None:
    @validate_args(x=lambda x: x > 0, y=(lambda y: y != "", "y must not be empty"))
    def f(x: int, y: str = "d") -> None: ...

    with pytest.raises(errors.ValidationError) as ei:
        f(-1)
    assert ei.value.parameter == "x"
    assert ei.value.value == -1
    assert "got -1" in str(ei.value)
    with pytest.raises(errors.ValidationError, match="y must not be empty"):
        f(1, "")
    with pytest.raises(TypeError):
        f()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        f(1, "a", 3)  # type: ignore[call-arg]


def test_validate_args_positional_only_keyword_only_and_variadic() -> None:
    @validate_args(a=lambda a: a > 0, b=lambda b: b > 0)
    def pos_only(a: int, b: int = 1, /) -> None: ...

    pos_only(1)
    pos_only(1, 2)
    with pytest.raises(errors.ValidationError):
        pos_only(1, -2)

    @validate_args(k=lambda k: k > 0)
    def kw_only(a: int, *, k: int = 1) -> None: ...

    kw_only(0)
    kw_only(0, k=3)
    with pytest.raises(errors.ValidationError):
        kw_only(0, k=-3)

    @validate_args(rest=lambda r: len(r) == 2, extra=lambda e: "z" in e)
    def variadic(a: int, *rest: int, **extra: int) -> None: ...

    variadic(1, 2, 3, z=1)
    with pytest.raises(errors.ValidationError):
        variadic(1, 2)


def test_validate_args_methods() -> None:
    class C:
        @validate_args(x=lambda x: x > 0)
        def m(self, x: int, y: int = 0) -> int:
            return x + y

        @classmethod
        @validate_args(x=lambda x: x > 0)
        def c(cls, x: int) -> int:
            return x

        @staticmethod
        @validate_args(x=lambda x: x > 0)
        def s(x: int) -> int:
            return x

    assert C().m(1) == 1
    assert C().m(1, 2) == 3
    assert C.c(2) == 2
    assert C.s(3) == 3
    for call in (lambda: C().m(0), lambda: C.c(0), lambda: C.s(0)):
        with pytest.raises(errors.ValidationError):
            call()


def test_validate_args_mutable_default_is_the_real_default() -> None:
    got: list[object] = []

    def keep(v: Any) -> bool:
        got.append(v)
        return True

    @validate_args(items=keep)
    def f(items: list[int] = []) -> None: ...  # noqa: B006

    f()
    assert got[0] is f.__wrapped__.__defaults__[0]  # type: ignore[attr-defined]


def test_check_type_fast_path_variants() -> None:
    @check_type(a=int, c=(str, bytes))
    def f(a: int, b: object = None, c: str = "x") -> None: ...

    f(1)
    f(1, 2)
    f(1, 2, b"x")
    f(1, c="s")
    f(a=1, b=0)
    with pytest.raises(errors.ArgumentTypeError) as ei:
        f("no")  # type: ignore[arg-type]
    assert ei.value.parameter == "a"
    assert "expected int, got str" in str(ei.value)
    with pytest.raises(errors.ArgumentTypeError, match="c"):
        f(1, 2, 3)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        f()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        f(1, 2, "c", 4)  # type: ignore[call-arg]


def test_check_type_positional_only_methods_and_kwonly() -> None:
    @check_type(a=int)
    def pos_only(a: int, /, b: int = 0) -> None: ...

    pos_only(1)
    pos_only(1, "free")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError):
        pos_only("x")  # type: ignore[arg-type]

    class C:
        @check_type(x=int)
        def m(self, x: int) -> int:
            return x

    assert C().m(1) == 1
    with pytest.raises(errors.ArgumentTypeError):
        C().m("1")  # type: ignore[arg-type]

    @check_type(k=int)
    def kw(a: int, *, k: int = 0) -> None: ...

    kw(1, k=2)
    with pytest.raises(errors.ArgumentTypeError):
        kw(1, k="2")  # type: ignore[arg-type]


def test_enforce_types_fast_path_numeric_tower_and_messages() -> None:
    @enforce_types
    def f(a: int, b: float = 1.0, c: complex = 0j, d: str | None = None) -> bool:
        return True

    assert f(1)
    assert f(True, 2, 3, None)  # bool is an int; int is a float and a complex
    assert f(1, 2.5, 1.5, "s")
    with pytest.raises(errors.ArgumentTypeError, match=r"parameter 'a' expected int, got str"):
        f("x")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match=r"parameter 'b' expected float, got str"):
        f(1, "x")  # type: ignore[arg-type]
    # 3.10 spells the hint Optional[str]; later versions print str | None.
    optional_str = r"parameter 'd' expected (str \| None|Optional\[str\])"
    with pytest.raises(errors.ArgumentTypeError, match=optional_str):
        f(1, 1.0, 0j, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        f()  # type: ignore[call-arg]


def test_enforce_types_kwargs_defaults_varargs_keyword_only_positional_only() -> None:
    @enforce_types
    def f(a: int, /, b: int = 0, *args: int, k: str = "k", **kw: float) -> int:
        return a

    assert f(1) == 1
    assert f(1, 2, 3, 4, k="z", x=1.5) == 1
    with pytest.raises(errors.ArgumentTypeError, match=r"args\[1\]"):
        f(1, 2, 3, "bad")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'k'"):
        f(1, k=1)  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match=r"kw\['x'\]"):
        f(1, x="s")  # type: ignore[arg-type]

    @enforce_types
    def g(a: int, b: int = 0) -> int:
        return a

    assert g(1, b=2) == 1
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'b'"):
        g(1, b="x")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'a'"):
        g(a="x")  # type: ignore[arg-type]


def test_enforce_types_methods_and_return() -> None:
    class C:
        @enforce_types
        def m(self, x: int) -> str:
            return str(x)

        @classmethod
        @enforce_types
        def c(cls, x: int) -> int:
            return x

        @staticmethod
        @enforce_types
        def s(x: int) -> int:
            return "no"  # type: ignore[return-value]

    assert C().m(1) == "1"
    assert C.c(2) == 2
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'x'"):
        C().m("1")  # type: ignore[arg-type]
    with pytest.raises(errors.ArgumentTypeError, match=r"return value expected int, got str"):
        C.s(1)


def test_enforce_types_return_quick_path_and_check_return_false() -> None:
    @enforce_types
    def f(x: int) -> float:
        return x  # an int is a valid float

    assert f(1) == 1

    @enforce_types
    def none_ret(x: int) -> None:
        return x  # type: ignore[return-value]

    with pytest.raises(errors.ArgumentTypeError, match="return value expected None, got int"):
        none_ret(1)

    @enforce_types(check_return=False)
    def unchecked(x: int) -> str:
        return x  # type: ignore[return-value]

    assert unchecked(1) == 1


def test_enforce_types_forward_refs_resolved_lazily_on_fast_path() -> None:
    @enforce_types
    def f(a: "Later", b: int = 0) -> "Later":
        return a

    # `Later` is defined only after decoration; the first call resolves it.
    globals()["Later"] = int
    try:
        assert f(1) == 1
        assert f(2, 3) == 2
        with pytest.raises(errors.ArgumentTypeError, match="parameter 'a' expected int"):
            f("x")  # type: ignore[arg-type]
    finally:
        del globals()["Later"]


def test_enforce_types_unresolved_forward_ref_retried_each_call() -> None:
    @enforce_types
    def f(a: "Missing", b: int = 0) -> int:  # noqa: F821
        return b

    assert f("anything") == 0  # unresolved: accepted, other params still checked
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'b'"):
        f("anything", "x")  # type: ignore[arg-type]
    globals()["Missing"] = int
    try:
        assert f(1) == 0  # resolved on a later call
        with pytest.raises(errors.ArgumentTypeError, match="parameter 'a'"):
            f("anything")
    finally:
        del globals()["Missing"]


def test_enforce_types_complex_hints_still_checked_after_quick_miss() -> None:
    @enforce_types
    def f(
        a: list[int], b: dict[str, int], c: type[int] = int, d: Callable[..., Any] = print
    ) -> None: ...

    f([1], {"a": 1})
    with pytest.raises(errors.ArgumentTypeError, match="list containing str at index 1"):
        f([1, "x"], {})  # type: ignore[list-item]
    with pytest.raises(errors.ArgumentTypeError, match="parameter 'd'"):
        f([1], {}, int, 3)  # type: ignore[arg-type]
