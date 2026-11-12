"""Per-instance state for methods, and one computation per cache key across threads.

By default ``throttle``, ``debounce`` and ``rate_limit`` keep their state on the decorated
function, so every instance of a class shares one window or budget. ``per_instance=True``
gives each instance its own state (held through a weak reference, so instances can still be
garbage collected) and ``key=`` splits it by the call's arguments. ``memoize`` gains
``singleflight=True`` so threads that miss on the same key run the function once.

Run: python examples/per_instance_state.py
"""

from __future__ import annotations

import gc
import threading
import time
import weakref

from python_wrap import RateLimitExceeded, debounce, memoize, rate_limit, throttle
from python_wrap.concurrency import limiter_of
from python_wrap.testing import fake_clock


class Sensor:
    def __init__(self, name: str) -> None:
        self.name = name

    # One window per sensor instead of one for the whole class.
    @throttle(60, per_instance=True)
    def read(self) -> str:
        return self.name


class Client:
    # Each client instance has its own request budget.
    @rate_limit(1, 60.0, per_instance=True)
    def call(self) -> str:
        return "ok"


class Document:
    def __init__(self) -> None:
        self.saved: list[str] = []

    # Each document debounces its own saves.
    @debounce(60, per_instance=True)
    def save(self, text: str) -> None:
        self.saved.append(text)


@throttle(60, key=lambda user, *_: user)
def notify(user: str, text: str) -> str:
    return f"{user}:{text}"


LOADS: list[int] = []


@memoize(singleflight=True)
def load_report(report_id: int) -> str:
    LOADS.append(report_id)
    time.sleep(0.05)  # a slow query
    return f"report-{report_id}"


def main() -> None:
    with fake_clock():
        a, b = Sensor("a"), Sensor("b")
        assert a.read() == "a"
        assert b.read() == "b"  # not skipped by a's window
        assert a.read() == "a"  # a's own window: skipped, previous result
        print("throttle(per_instance=True): each sensor has its own window")

        assert notify("ana", "1") == "ana:1"
        assert notify("ben", "1") == "ben:1"  # another key: runs
        assert notify("ana", "2") == "ana:1"  # same key inside the window: skipped
        print("throttle(key=): one window per user")

        c1, c2 = Client(), Client()
        assert c1.call() == "ok"
        assert c2.call() == "ok"  # its own budget
        try:
            c1.call()
        except RateLimitExceeded:
            pass
        else:
            raise AssertionError("c1 should be over its limit")
        limiter = limiter_of(Client.call, instance=c1)
        assert limiter is not None
        limiter.reset()
        assert c1.call() == "ok"
        print("rate_limit(per_instance=True): limiter_of(..., instance=) reaches one limiter")

    d1, d2 = Document(), Document()
    d1.save("draft one")
    d2.save("draft two")
    Document.save.flush(d1)  # flush/cancel take the call's arguments to pick the slot
    assert d1.saved == ["draft one"] and d2.saved == []
    Document.save.cancel(d2)
    assert d2.saved == []
    print("debounce(per_instance=True): flush and cancel one document")

    ref = weakref.ref(a)
    del a
    gc.collect()
    assert ref() is None  # the per-instance state did not keep the sensor alive
    print("instances stay collectable")

    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(load_report(7))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["report-7"] * 8
    assert LOADS == [7]  # eight concurrent misses, one computation
    print("memoize(singleflight=True): 8 threads, 1 computation")


if __name__ == "__main__":
    main()
