"""The public surface: every name is importable, and v0.1 call forms keep working."""

import inspect
import warnings
from datetime import timedelta

import pytest

import python_wrap
from python_wrap import decorators

V01_NAMES = [
    "timing", "retry", "memoize", "log_args", "log_return", "validate_args", "cache",
    "once", "deprecate", "check_type", "retry_on_exception", "measure_memory", "profile",
    "rate_limit", "mock_data", "benchmark", "run_in_thread", "no_debug", "transactional",
    "revert_on_failure", "audit", "trace", "timeout",
]  # fmt: skip


def test_all_names_resolve():
    for name in python_wrap.__all__:
        assert hasattr(python_wrap, name), name


@pytest.mark.parametrize("name", V01_NAMES)
def test_v01_names_still_importable(name):
    assert hasattr(python_wrap, name)
    assert getattr(decorators, name) is getattr(python_wrap, name)


def test_v01_readme_forms(fake_clock):
    calls = []

    @python_wrap.retry(max_attempts=3, delay=1.0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError
        return "ok"

    assert flaky() == "ok"
    assert python_wrap.retry(lambda: 5, max_attempts=2, delay=0)() == 5
    assert python_wrap.timing(lambda: 1)() == 1
    assert python_wrap.memoize(lambda n: n * 2)(4) == 8
    assert python_wrap.cache(timedelta(minutes=5))(lambda: 3)() == 3
    assert python_wrap.timeout(5)(lambda: 4)() == 4

    with pytest.raises(ValueError):
        python_wrap.validate_args(x=lambda x: x > 0)(lambda x: x)(-1)
    with pytest.raises(TypeError):
        python_wrap.check_type(name=str)(lambda name: name)(1)


def test_decorators_preserve_metadata_and_async_nature():
    async def coro(x: int) -> int:
        return x

    for deco in (python_wrap.timing, python_wrap.memoize, python_wrap.retry, python_wrap.once):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wrapped = deco(coro)
        assert inspect.iscoroutinefunction(wrapped), deco
        assert wrapped.__name__ == "coro"
        assert inspect.signature(wrapped) == inspect.signature(coro)
