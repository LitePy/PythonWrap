"""Shared fixtures.

``fake_clock`` replaces every time source in ``python_wrap._core`` (via the public
``python_wrap.testing`` helpers) so time-based decorators can be tested instantly:
``sleep``/``async_sleep`` advance the clock instead of waiting.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from python_wrap.testing import FakeClock as FakeClock
from python_wrap.testing import fake_clock as _fake_clock


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    with _fake_clock() as clock:
        yield clock
