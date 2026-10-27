"""Tests for python_wrap.devtools."""

from __future__ import annotations

import functools
import inspect
import os
from typing import Any

import pytest

from python_wrap.devtools import compose, deprecate, mock_data, no_debug


def _deprecation_text(obj: Any) -> str:
    return str(obj.__deprecated__)


# --- deprecate -------------------------------------------------------------


def test_legacy_message_form_warns_and_returns_value() -> None:
    @deprecate("Use new_func instead")
    def old(x: int) -> int:
        return x * 2

    with pytest.warns(DeprecationWarning, match="Use new_func instead") as record:
        assert old(3) == 6
    assert str(record[0].message) == "Use new_func instead"


def test_bare_decorator_generates_message() -> None:
    @deprecate
    def old() -> str:
        return "ok"

    with pytest.warns(DeprecationWarning) as record:
        assert old() == "ok"
    assert str(record[0].message) == f"{old.__qualname__} is deprecated."


@pytest.mark.parametrize(
    ("kwargs", "suffix"),
    [
        ({"since": "0.2"}, " since 0.2."),
        ({"removed_in": "1.0"}, " and will be removed in 1.0."),
        ({"alternative": "new_func"}, "; use new_func instead."),
        (
            {"since": "0.2", "removed_in": "1.0", "alternative": "new_func"},
            " since 0.2 and will be removed in 1.0; use new_func instead.",
        ),
    ],
)
def test_generated_message_combines_given_parts(kwargs: dict[str, str], suffix: str) -> None:
    @deprecate(**kwargs)
    def old_func() -> None:
        return None

    with pytest.warns(DeprecationWarning) as record:
        old_func()
    assert str(record[0].message) == f"{old_func.__qualname__} is deprecated{suffix}"


def test_explicit_message_replaces_generated_text() -> None:
    @deprecate("custom text", since="0.2", alternative="new_func")
    def old() -> None:
        return None

    with pytest.warns(DeprecationWarning) as record:
        old()
    assert str(record[0].message) == "custom text"
    assert _deprecation_text(old) == "custom text"


def test_warns_on_every_call() -> None:
    @deprecate("again")
    def old() -> None:
        return None

    with pytest.warns(DeprecationWarning) as record:
        old()
        old()
    assert len(record) == 2


def test_warning_points_at_caller_file() -> None:
    @deprecate("moved")
    def old() -> None:
        return None

    with pytest.warns(DeprecationWarning) as record:
        old()
    assert record[0].filename == __file__


def test_custom_category() -> None:
    @deprecate("soon", category=FutureWarning)
    def old() -> None:
        return None

    with pytest.warns(FutureWarning, match="soon"):
        old()


def test_sets_pep702_attribute_and_keeps_metadata() -> None:
    @deprecate("gone")
    def old_named() -> int:
        """Docs."""
        return 1

    assert _deprecation_text(old_named) == "gone"
    assert old_named.__name__ == "old_named"
    assert old_named.__doc__ == "Docs."


async def test_async_function_warns_on_await_and_stays_coroutine() -> None:
    @deprecate("async gone")
    async def old(x: int) -> int:
        return x + 1

    assert inspect.iscoroutinefunction(old)
    with pytest.warns(DeprecationWarning, match="async gone") as record:
        assert await old(1) == 2
    assert record[0].filename == __file__


def test_methods_instance_class_and_static() -> None:
    class Service:
        @deprecate("instance gone")
        def run(self) -> str:
            return "run"

        @classmethod
        @deprecate("class gone")
        def make(cls) -> str:
            return cls.__name__

        @staticmethod
        @deprecate("static gone")
        def helper() -> int:
            return 7

        @deprecate
        @staticmethod
        def helper_bare() -> int:
            return 8

    svc = Service()
    with pytest.warns(DeprecationWarning, match="instance gone"):
        assert svc.run() == "run"
    with pytest.warns(DeprecationWarning, match="class gone"):
        assert Service.make() == "Service"
    with pytest.warns(DeprecationWarning, match="static gone"):
        assert Service.helper() == 7
    with pytest.warns(DeprecationWarning, match="static gone"):
        assert svc.helper() == 7
    with pytest.warns(DeprecationWarning, match="is deprecated"):
        assert Service.helper_bare() == 8


def test_class_stays_class_and_warns_on_init() -> None:
    @deprecate("Widget is going away")
    class Widget:
        def __init__(self, size: int) -> None:
            self.size = size

    assert isinstance(Widget, type)
    assert _deprecation_text(Widget) == "Widget is going away"
    with pytest.warns(DeprecationWarning, match="going away") as record:
        widget = Widget(3)
    assert widget.size == 3
    assert isinstance(widget, Widget)
    assert record[0].filename == __file__


def test_subclass_instances_still_pass_isinstance() -> None:
    @deprecate("Base is going away")
    class Base:
        pass

    class Child(Base):
        pass

    with pytest.warns(DeprecationWarning):
        child = Child()
    assert isinstance(child, Base)


def test_bare_class_decorator_without_init() -> None:
    @deprecate
    class Empty:
        pass

    with pytest.warns(DeprecationWarning, match="is deprecated"):
        assert isinstance(Empty(), Empty)


# --- mock_data -------------------------------------------------------------


def test_mock_data_returns_value_without_calling_function() -> None:
    calls: list[int] = []

    @mock_data(42)
    def fetch() -> int:
        calls.append(1)
        return 1

    assert fetch() == 42
    assert calls == []


def test_callable_data_is_called_on_each_call() -> None:
    counter = iter(range(10))

    @mock_data(lambda: next(counter))
    def value() -> int:
        return -1

    assert value() == 0
    assert value() == 1


def test_lambda_wrapper_returns_a_callable_itself() -> None:
    def handler() -> str:
        return "handled"

    @mock_data(lambda: handler)
    def get_handler() -> Any:
        return None

    assert get_handler() is handler


def test_enabled_false_runs_real_function() -> None:
    @mock_data(42, enabled=False)
    def add_one(x: int) -> int:
        return x + 1

    assert add_one(1) == 2


def test_enabled_callable_is_evaluated_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK", "1")

    @mock_data("fake", enabled=lambda: os.environ.get("MOCK") == "1")
    def fetch() -> str:
        return "real"

    assert fetch() == "fake"
    monkeypatch.setenv("MOCK", "0")
    assert fetch() == "real"
    monkeypatch.setenv("MOCK", "1")
    assert fetch() == "fake"


def test_mock_preserves_metadata_and_signature() -> None:
    @mock_data("fake")
    def fetch(user_id: int, *, verbose: bool = False) -> str:
        """Fetch a user."""
        return "real"

    assert fetch.__name__ == "fetch"
    assert fetch.__doc__ == "Fetch a user."
    assert list(inspect.signature(fetch).parameters) == ["user_id", "verbose"]


async def test_async_mock_returns_data_and_skips_real_call() -> None:
    @mock_data("fake")
    async def fetch() -> str:
        raise AssertionError("real call should not run")

    assert inspect.iscoroutinefunction(fetch)
    assert await fetch() == "fake"


async def test_async_disabled_awaits_real_function() -> None:
    @mock_data("fake", enabled=False)
    async def times_ten(x: int) -> int:
        return x * 10

    assert await times_ten(2) == 20


# --- no_debug --------------------------------------------------------------


def test_no_debug_is_passthrough_and_warns_at_decoration() -> None:
    def func() -> int:
        return 1

    with pytest.warns(DeprecationWarning, match="removed in 0.3"):
        decorated = no_debug(func)
    assert decorated is func


def test_no_debug_bare_and_call_forms_warn_at_decoration() -> None:
    with pytest.warns(DeprecationWarning, match="logging") as record:

        @no_debug
        def bare() -> int:
            return 1

    assert record[0].filename == __file__
    assert bare() == 1

    with pytest.warns(DeprecationWarning, match="removed in 0.3"):

        @no_debug()
        def called() -> int:
            return 2

    assert called() == 2


# --- compose ---------------------------------------------------------------


def _recording(log: list[str], name: str) -> Any:
    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            log.append(f"{name}-in")
            result = func(*args, **kwargs)
            log.append(f"{name}-out")
            return result

        return wrapper

    return decorator


def test_compose_applies_like_stacked_decorators() -> None:
    log: list[str] = []

    @compose(_recording(log, "a"), _recording(log, "b"), _recording(log, "c"))
    def f() -> str:
        log.append("f")
        return "done"

    assert f() == "done"
    assert log == ["a-in", "b-in", "c-in", "f", "c-out", "b-out", "a-out"]


def test_compose_without_arguments_is_identity() -> None:
    def f() -> int:
        return 1

    assert compose()(f) is f


def test_compose_combines_deprecate_and_mock_data() -> None:
    @compose(deprecate("old api"), mock_data(7))
    def value() -> int:
        return 1

    with pytest.warns(DeprecationWarning, match="old api"):
        assert value() == 7


def test_compose_rejects_non_callable_arguments() -> None:
    with pytest.raises(TypeError, match="argument 1"):
        compose(lambda f: f, 42)  # type: ignore[arg-type]


# --- deprecate on classes without their own __init__ -------------------------


def test_deprecate_float_subclass_keeps_construction() -> None:
    @deprecate("old")
    class Meters(float):
        pass

    with pytest.warns(DeprecationWarning, match="old") as rec:
        m = Meters(3.0)
    assert m == 3.0 and isinstance(m, float)
    assert len(rec) == 1


def test_deprecate_namedtuple() -> None:
    from typing import NamedTuple

    @deprecate
    class P(NamedTuple):
        x: int

    with pytest.warns(DeprecationWarning) as rec:
        p = P(1)
    assert p.x == 1
    assert len(rec) == 1


def test_deprecate_dataclass_and_regular_class() -> None:
    import dataclasses

    @deprecate
    @dataclasses.dataclass
    class D:
        x: int

    class Plain:
        def __init__(self, v: int) -> None:
            self.v = v

    Plain = deprecate(Plain)

    with pytest.warns(DeprecationWarning) as rec:
        assert D(2).x == 2
        assert Plain(3).v == 3
    assert len(rec) == 2


def test_deprecate_class_without_any_init_or_new() -> None:
    @deprecate
    class Bare:
        pass

    with pytest.warns(DeprecationWarning):
        Bare()
    with pytest.raises(TypeError), pytest.warns(DeprecationWarning):
        Bare(1)  # type: ignore[call-arg]


def test_deprecate_subclass_of_deprecated_class_warns_once() -> None:
    @deprecate("old")
    class Meters(float):
        pass

    class Feet(Meters):
        pass

    with pytest.warns(DeprecationWarning) as rec:
        f = Feet(2.0)
    assert f == 2.0
    assert len(rec) == 1
