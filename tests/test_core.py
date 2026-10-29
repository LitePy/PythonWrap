import functools
import logging
import subprocess
import sys

import python_wrap
from python_wrap import _core


def test_import_does_not_configure_root_logging():
    # A fresh interpreter, so handlers added by pytest or other tests cannot interfere.
    code = (
        "import logging, python_wrap;"
        "assert logging.getLogger().handlers == [], logging.getLogger().handlers;"
        "assert any(isinstance(h, logging.NullHandler)"
        " for h in logging.getLogger('python_wrap').handlers)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_get_logger_resolution():
    custom = logging.getLogger("custom")
    assert _core.get_logger(custom) is custom
    assert _core.get_logger("other").name == "other"
    assert _core.get_logger().name == "python_wrap"


def test_is_async_sees_through_partial():
    async def coro(x):
        return x

    def plain(x):
        return x

    assert _core.is_async(coro)
    assert _core.is_async(functools.partial(coro, 1))
    assert not _core.is_async(plain)


def test_version_exposed():
    from packaging.version import Version

    assert str(Version(python_wrap.__version__)) == python_wrap.__version__


class _AsyncCallable:
    async def __call__(self, x):
        return x


class _SyncCallable:
    def __call__(self, x):
        return x


def test_is_async_detects_callable_objects():
    assert _core.is_async(_AsyncCallable())
    assert _core.is_async(functools.partial(_AsyncCallable(), 1))
    assert _core.is_async(_AsyncCallable().__call__)
    assert not _core.is_async(_SyncCallable())
    # A class is callable but calling it constructs an instance: never async.
    assert not _core.is_async(_AsyncCallable)
    assert not _core.is_async(_SyncCallable)
    assert not _core.is_async(len)


def test_is_async_callable_instance_with_descriptor() -> None:
    class Spyish:
        async def __call__(self) -> None: ...

        def __get__(self, obj: object, owner: object = None) -> object:
            return self

    assert _core.is_async(Spyish())


def test_overrides_defaults_and_set_overrides_returns_previous() -> None:
    assert _core.overrides == _core.Overrides()
    previous = _core.set_overrides(no_sleep=True, max_attempts=3)
    try:
        assert previous == {"no_sleep": False, "max_attempts": None}
        assert _core.overrides.no_sleep and _core.overrides.max_attempts == 3
    finally:
        _core.set_overrides(**previous)
    assert _core.overrides == _core.Overrides()


def test_set_overrides_rejects_unknown_and_invalid() -> None:
    import pytest

    with pytest.raises(TypeError, match="bogus"):
        _core.set_overrides(bogus=True)
    with pytest.raises(ValueError):
        _core.set_overrides(max_attempts=0)
    assert _core.overrides == _core.Overrides()


def test_sleep_honours_no_sleep(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    import time

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _core.sleep(1)
    previous = _core.set_overrides(no_sleep=True)
    try:
        _core.sleep(2)
        asyncio.run(_core.async_sleep(3))
    finally:
        _core.set_overrides(**previous)
    assert slept == [1]
