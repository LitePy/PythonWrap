"""Cooperative cancellation for a sync ``timeout``.

``timeout`` cannot stop a sync function that runs in a worker thread (Windows, non-main
threads, ``use_signals=False``): the caller gets ``TimeoutExpired`` while the function keeps
going and its side effects continue. Inside the function, ``checkpoint()`` and
``cancel_requested()`` let it stop itself once the limit has fired. The script also shows
``foreign_alarm_state()`` and the doctor check for a ``SIGALRM`` that something else owns.

Run: python examples/cooperative_timeout.py
"""

from __future__ import annotations

import contextlib
import threading
import time

from python_wrap import (
    TimeoutExpired,
    cancel_requested,
    checkpoint,
    current_token,
    timeout,
)
from python_wrap.concurrency import foreign_alarm_state

written: list[int] = []
stopped = threading.Event()


# 1. checkpoint() raises TimeoutExpired inside the worker once the limit is over, so the
#    loop ends instead of writing rows forever. use_signals=False forces the worker-thread
#    strategy, which behaves the same on every platform.
@timeout(0.05, use_signals=False)
def import_rows() -> None:
    try:
        for row in range(10_000):
            checkpoint()
            written.append(row)
            time.sleep(0.01)
    finally:
        stopped.set()


# 2. cancel_requested() is a plain boolean for code that wants to clean up rather than raise.
polled: list[str] = []


@timeout(0.05, use_signals=False)
def sync_files() -> None:
    token = current_token()
    assert token is not None and not cancel_requested()
    while not cancel_requested():
        time.sleep(0.01)
    polled.append("noticed")
    time.sleep(0.01)
    polled.append("cleaned up")


def main() -> None:
    try:
        import_rows()
    except TimeoutExpired as exc:
        assert exc.seconds == 0.05
        print(f"import_rows timed out after {len(written)} rows")
    else:
        raise AssertionError("expected a timeout")
    assert stopped.wait(2), "the worker should stop itself at the next checkpoint"
    rows_at_stop = len(written)
    time.sleep(0.05)
    assert len(written) == rows_at_stop  # nothing is written after the function ended
    assert 0 < rows_at_stop < 100
    print("worker stopped itself; no writes after the timeout")

    with contextlib.suppress(TimeoutExpired):
        sync_files()
    deadline = time.monotonic() + 2
    while "cleaned up" not in polled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert polled == ["noticed", "cleaned up"]
    print("cancel_requested() let the function clean up:", polled)

    # Outside any timeout both helpers are no-ops.
    checkpoint()
    assert not cancel_requested() and current_token() is None

    # 3. Is another SIGALRM user in the way? None on a clean process (and where there is
    #    no SIGALRM); a description string otherwise.
    state = foreign_alarm_state()
    assert state is None or isinstance(state, str)
    print("foreign SIGALRM state:", state)


if __name__ == "__main__":
    main()
