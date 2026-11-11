"""Story: a service class caches expensive reads, with per-instance throttles."""

from __future__ import annotations

import threading
import time

import pytest

from python_wrap import RateLimitExceeded, memoize, rate_limit, throttle
from python_wrap.testing import FakeClock

pytestmark = pytest.mark.e2e


def test_concurrent_misses_run_the_loader_once() -> None:
    loads: list[int] = []

    @memoize(singleflight=True)
    def load_report(report_id: int) -> str:
        loads.append(report_id)
        time.sleep(0.05)  # a slow query
        return f"report-{report_id}"

    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(load_report(7))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["report-7"] * 8
    assert loads == [7]


def test_cached_value_expires_after_its_ttl(fake_clock: FakeClock) -> None:
    calls: list[str] = []

    @memoize(ttl=60)
    def price(sku: str) -> int:
        calls.append(sku)
        return len(calls)

    assert price("a") == price("a") == 1
    fake_clock.advance(61)
    assert price("a") == 2
    assert calls == ["a", "a"]


def test_instances_keep_independent_state(fake_clock: FakeClock) -> None:
    class Sensor:
        def __init__(self, name: str) -> None:
            self.name = name

        @throttle(60, per_instance=True)
        def read(self) -> str:
            return self.name

    class Client:
        @rate_limit(1, 60.0, per_instance=True)
        def call(self) -> str:
            return "ok"

    a, b = Sensor("a"), Sensor("b")
    assert (a.read(), b.read()) == ("a", "b")  # b is not skipped by a's window

    c1, c2 = Client(), Client()
    assert c1.call() == c2.call() == "ok"  # each has its own budget
    with pytest.raises(RateLimitExceeded):
        c1.call()
    fake_clock.advance(61)
    assert c1.call() == "ok"
