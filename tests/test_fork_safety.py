"""Run fork regressions in a disposable interpreter so hangs cannot wedge pytest."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")


def test_fork_resets_inherited_locks_cache_flights_and_periodic():
    script = """
import os, threading, logging
from python_wrap import memoize, once, singleflight, periodic, RateLimiter, Bulkhead, CircuitBreaker
from python_wrap._fork import ForkSafeLock, ForkSafeRLock
locks = [ForkSafeLock(), ForkSafeRLock()]
ready, finish = threading.Event(), threading.Event()
def holder():
    with locks[0], locks[1]:
        ready.set(); finish.wait(5)
thread = threading.Thread(target=holder); thread.start(); assert ready.wait(2)
calls = []
@memoize
def cached(): calls.append(1); return len(calls)
assert cached() == 1
limiter = RateLimiter(1, 60)
assert limiter.try_acquire()
@periodic(60)
def background(): pass
background.start()
bulkhead = Bulkhead(1, timeout=0)
bulkhead.acquire()
breaker = CircuitBreaker()
breaker.force_open()
parent = os.getpid()
started, release = threading.Event(), threading.Event()
@singleflight
def flight():
    if os.getpid() == parent:
        started.set(); release.wait(5)
    return os.getpid()
leader = threading.Thread(target=flight); leader.start(); assert started.wait(2)
pid = os.fork()
if pid == 0:
    try:
        for lock in locks:
            assert lock.acquire(False); lock.release()
        assert cached() == 2
        assert limiter.try_acquire()
        assert flight() == os.getpid()
        bulkhead.acquire(); bulkhead.release()
        assert breaker.state.value == 'closed'
        assert not background.running
        assert background.stop()
    except BaseException:
        import traceback; traceback.print_exc(); os._exit(1)
    os._exit(0)
_, status = os.waitpid(pid, 0)
finish.set(); release.set(); thread.join(); leader.join(); background.stop(); bulkhead.release()
assert os.waitstatus_to_exitcode(status) == 0
assert cached() == 1 and not limiter.try_acquire()
"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fork_registry_does_not_retain_decorators():
    import gc
    import weakref

    from python_wrap import memoize
    from python_wrap._fork import ForkSafeLock

    lock = ForkSafeLock()
    ref = weakref.ref(lock)
    del lock
    gc.collect()
    assert ref() is None

    def create():
        @memoize
        def work():
            return 1

        return weakref.ref(work)

    ref = create()
    gc.collect()
    assert ref() is None
