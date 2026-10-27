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
    assert python_wrap.__version__.startswith("0.2")


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
