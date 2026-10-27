"""Shared fixtures.

``fake_clock`` replaces every time source in ``python_wrap._core`` so time-based
decorators can be tested instantly and deterministically: ``sleep``/``async_sleep``
advance the clock instead of waiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from python_wrap import _core


@dataclass
class FakeClock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.0)

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(_core, "monotonic", clock.time)
    monkeypatch.setattr(_core, "perf_counter", clock.time)
    monkeypatch.setattr(_core, "sleep", clock.sleep)
    monkeypatch.setattr(_core, "async_sleep", clock.async_sleep)
    return clock
