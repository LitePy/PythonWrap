from __future__ import annotations

import asyncio
import gc
import inspect
import threading
import time

import pytest

from python_wrap.batching import batch_calls
from python_wrap.errors import BatchError, PythonWrapError


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.001)


def only_current_task():
    return asyncio.all_tasks() == {asyncio.current_task()}


# --------------------------------------------------------------------------- options


@pytest.mark.parametrize(
    "kwargs",
    [{"max_size": 0}, {"max_size": 1.5}, {"max_size": True}, {"max_wait": -1}],
)
def test_validates_options(kwargs):
    with pytest.raises(ValueError):
        batch_calls(**kwargs)


def test_batch_error_is_a_python_wrap_error():
    assert issubclass(BatchError, PythonWrapError)


# --------------------------------------------------------------------------- async


async def test_async_coalesces_concurrent_calls(fake_clock):
    calls = []

    @batch_calls()
    async def load(ids: list[int]) -> list[str]:
        calls.append(list(ids))
        return [f"u{i}" for i in ids]

    assert inspect.iscoroutinefunction(load)
    assert load.__name__ == "load"
    results = await asyncio.gather(load(1), load(2), load(3))
    assert results == ["u1", "u2", "u3"]
    assert calls == [[1, 2, 3]]
    assert fake_clock.sleeps == [0.01]
    assert load.pending == 0
    assert only_current_task()


async def test_async_mapping_result_with_key_and_deduplication(fake_clock):
    calls = []

    @batch_calls(key=lambda user: user["id"])
    async def load(users: list[dict]) -> dict[int, str]:
        calls.append([u["id"] for u in users])
        return {u["id"]: u["name"].upper() for u in users} | {99: "extra is ignored"}

    a, b, c = await asyncio.gather(
        load({"id": 1, "name": "ann"}),
        load({"id": 2, "name": "bob"}),
        load({"id": 1, "name": "ann"}),
    )
    assert (a, b, c) == ("ANN", "BOB", "ANN")
    assert calls == [[1, 2]]


async def test_async_max_size_splits_batches(fake_clock):
    calls = []

    @batch_calls(max_size=2)
    async def load(ids: list[int]) -> tuple[int, ...]:
        calls.append(list(ids))
        return tuple(i * 10 for i in ids)

    assert await asyncio.gather(*(load(i) for i in range(5))) == [0, 10, 20, 30, 40]
    assert calls == [[0, 1], [2, 3], [4]]
    await asyncio.sleep(0)  # let the cancelled window timers finish
    assert only_current_task()


async def test_async_exception_reaches_every_caller(fake_clock):
    @batch_calls()
    async def load(ids: list[int]) -> list[int]:
        raise KeyError("down")

    results = await asyncio.gather(load(1), load(2), return_exceptions=True)
    assert all(isinstance(r, KeyError) for r in results)
    assert results[0] is results[1]


async def test_async_wrong_length_is_a_batch_error_for_everyone(fake_clock):
    @batch_calls()
    async def load(ids: list[int]) -> list[int]:
        return [1]

    results = await asyncio.gather(load(1), load(2), return_exceptions=True)
    assert all(isinstance(r, BatchError) for r in results)
    assert "returned 1 results for 2 items" in str(results[0])


async def test_async_missing_key_only_fails_that_caller(fake_clock):
    @batch_calls()
    async def load(ids: list[int]) -> dict[int, int]:
        return {i: i for i in ids if i != 2}

    results = await asyncio.gather(load(1), load(2), load(3), return_exceptions=True)
    assert results[0] == 1 and results[2] == 3
    assert isinstance(results[1], BatchError)
    assert "no entry for key 2" in str(results[1])


@pytest.mark.parametrize("bad", ["ab", 42, None, iter([1, 2])])
async def test_async_non_sequence_result_is_a_batch_error(fake_clock, bad):
    @batch_calls()
    async def load(ids: list[int]):
        return bad

    results = await asyncio.gather(load(1), load(2), return_exceptions=True)
    assert all(isinstance(r, BatchError) for r in results)
    assert "must return a sequence or a mapping" in str(results[0])


async def test_async_cancelled_caller_drops_its_unsent_item():
    calls = []

    @batch_calls(max_wait=10)
    async def load(ids: list[int]) -> list[int]:
        calls.append(list(ids))
        return ids

    a = asyncio.create_task(load(1))
    b = asyncio.create_task(load(2))
    b2 = asyncio.create_task(load(2))
    await asyncio.sleep(0)
    assert load.pending == 2
    b.cancel()
    await asyncio.sleep(0)
    assert load.pending == 2  # b2 still wants item 2
    b2.cancel()
    await asyncio.sleep(0)
    assert load.pending == 1
    await load.aflush()
    assert await a == 1
    assert calls == [[1]]
    with pytest.raises(asyncio.CancelledError):
        await b


async def test_async_all_callers_cancelled_means_no_batch():
    calls = []

    @batch_calls(max_wait=10)
    async def load(ids: list[int]) -> list[int]:
        calls.append(ids)
        return ids

    task = asyncio.create_task(load(1))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert calls == []
    assert load.pending == 0
    assert only_current_task()


async def test_async_cancelling_one_caller_after_dispatch_spares_the_others(fake_clock):
    started = asyncio.Event()
    release = asyncio.Event()

    @batch_calls()
    async def load(ids: list[int]) -> list[int]:
        started.set()
        await release.wait()
        raise ValueError("late failure")

    a = asyncio.create_task(load(1))
    b = asyncio.create_task(load(2))
    await started.wait()
    a.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(ValueError):
        await b
    with pytest.raises(asyncio.CancelledError):
        await a
    gc.collect()  # an unretrieved exception would be reported here (filterwarnings=error)


async def test_async_aflush_sends_now_and_waits():
    @batch_calls(max_wait=10)
    async def load(ids: list[int]) -> list[int]:
        await asyncio.sleep(0)
        return [i + 1 for i in ids]

    await load.aflush()  # nothing pending on this loop: no-op
    tasks = [asyncio.create_task(load(i)) for i in range(3)]
    await asyncio.sleep(0)
    assert load.pending == 3
    await load.aflush()
    assert load.pending == 0
    assert all(t.done() for t in tasks)
    assert [t.result() for t in tasks] == [1, 2, 3]
    assert only_current_task()


async def test_async_aflush_on_a_fresh_loop_is_a_noop():
    @batch_calls()
    async def load(ids: list[int]) -> list[int]:
        return ids

    await load.aflush()
    assert await load(1) == 1
    await load.aflush()  # loop state exists, nothing queued or running


async def test_async_methods_batch_per_instance(fake_clock):
    seen = []

    class Repo:
        def __init__(self, name):
            self.name = name

        @batch_calls()
        async def get(self, ids: list[int]) -> list[str]:
            seen.append((self.name, list(ids)))
            return [f"{self.name}{i}" for i in ids]

        @classmethod
        @batch_calls()
        async def cget(cls, ids: list[int]) -> list[str]:
            return [f"{cls.__name__}{i}" for i in ids]

    a, b = Repo("a"), Repo("b")
    results = await asyncio.gather(a.get(1), b.get(2), a.get(3), Repo.cget(4))
    assert results == ["a1", "b2", "a3", "Repo4"]
    assert sorted(seen) == [("a", [1, 3]), ("b", [2])]
    assert a.get.pending == 0


async def test_async_base_exception_propagates_from_the_batch_task(fake_clock):
    class Stop(BaseException):
        pass

    @batch_calls()
    async def load(ids: list[int]) -> list[int]:
        raise Stop

    with pytest.raises(Stop):
        await load(1)
    await asyncio.sleep(0)


async def test_async_batch_task_cancelled_cancels_callers():
    started = asyncio.Event()

    @batch_calls(max_wait=10)
    async def load(ids: list[int]) -> list[int]:
        started.set()
        await asyncio.Event().wait()
        return ids

    caller = asyncio.create_task(load(1))
    await asyncio.sleep(0)
    flush = asyncio.create_task(load.aflush())
    await started.wait()
    for task in asyncio.all_tasks():
        if task not in (asyncio.current_task(), caller, flush):
            task.cancel()  # the batch task itself
    with pytest.raises(asyncio.CancelledError):
        await caller
    await flush


def test_async_batches_are_per_event_loop():
    calls = []

    @batch_calls(max_wait=0)
    async def load(ids: list[int]) -> list[int]:
        calls.append(list(ids))
        return ids

    async def main(base):
        return await asyncio.gather(load(base), load(base + 1))

    assert asyncio.run(main(1)) == [1, 2]
    assert asyncio.run(main(10)) == [10, 11]
    assert calls == [[1, 2], [10, 11]]


async def test_async_unhashable_item_needs_a_key():
    @batch_calls()
    async def load(items: list[list[int]]) -> list[int]:
        return [len(i) for i in items]

    with pytest.raises(TypeError, match="not hashable"):
        await load([1, 2])


# --------------------------------------------------------------------------- sync


def run_threads(n, target):
    results = [None] * n
    errors = [None] * n

    def worker(i):
        try:
            results[i] = target(i)
        except BaseException as exc:
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    return results, errors


def test_sync_threads_fill_a_batch_and_share_one_call():
    calls = []
    before = threading.active_count()

    @batch_calls(max_size=4, max_wait=10)
    def load(ids: list[int]) -> list[int]:
        calls.append(sorted(ids))
        return [i * 2 for i in ids]

    results, errors = run_threads(4, load)
    assert errors == [None] * 4
    assert results == [0, 2, 4, 6]
    assert calls == [[0, 1, 2, 3]]
    assert load.pending == 0
    assert threading.active_count() == before


def test_sync_window_closes_after_max_wait():
    calls = []

    @batch_calls(max_wait=0.01)
    def load(ids: list[int]) -> list[str]:
        calls.append((list(ids), threading.current_thread()))
        return [str(i) for i in ids]

    assert load(7) == "7"
    assert calls == [([7], threading.current_thread())]  # ran by the caller itself


def test_sync_flush_sends_now_from_the_calling_thread():
    ran_in = []

    @batch_calls(max_wait=10)
    def load(ids: list[int]) -> dict[int, int]:
        ran_in.append(threading.current_thread())
        return {i: -i for i in ids}

    load.flush()  # nothing pending: no-op
    box = []
    worker = threading.Thread(target=lambda: box.append(load(5)))
    worker.start()
    wait_until(lambda: load.pending == 1)
    load.flush()
    worker.join(5)
    assert box == [-5]
    assert ran_in == [threading.current_thread()]


def test_sync_leader_timing_out_after_a_flush_started_waits_for_it():
    release = threading.Event()
    calls = []

    @batch_calls(max_wait=0.01)
    def load(ids: list[int]) -> list[int]:
        calls.append(list(ids))
        release.wait(5)
        return ids

    box = []
    worker = threading.Thread(target=lambda: box.append(load(1)))
    worker.start()
    wait_until(lambda: load.pending == 1)
    flusher = threading.Thread(target=load.flush)
    flusher.start()
    wait_until(lambda: calls)
    time.sleep(0.03)  # the worker's window expires while the flush is running
    release.set()
    worker.join(5)
    flusher.join(5)
    assert box == [1]
    assert calls == [[1]]


def test_sync_duplicates_share_a_result():
    calls = []

    @batch_calls(max_size=2, max_wait=10, key=str.lower)
    def load(names: list[str]) -> dict[str, int]:
        calls.append(list(names))
        return {n.lower(): len(calls) for n in names}

    results, _ = run_threads(3, lambda i: load(["A", "a", "b"][i]))
    assert results == [1, 1, 1]
    assert len(calls) == 1
    assert sorted(n.lower() for n in calls[0]) == ["a", "b"]


def test_sync_exception_reaches_every_caller():
    @batch_calls(max_size=3, max_wait=10)
    def load(ids: list[int]) -> list[int]:
        raise RuntimeError("db down")

    _, errors = run_threads(3, load)
    assert all(isinstance(e, RuntimeError) for e in errors)


def test_sync_contract_violations():
    @batch_calls(max_size=2, max_wait=10)
    def short(ids: list[int]) -> list[int]:
        return []

    _, errors = run_threads(2, short)
    assert all(isinstance(e, BatchError) for e in errors)

    @batch_calls(max_size=2, max_wait=10)
    def partial(ids: list[int]) -> dict[int, int]:
        return {0: 0}

    results, errors = run_threads(2, partial)
    assert results[0] == 0 and errors[0] is None
    assert isinstance(errors[1], BatchError)

    @batch_calls(max_size=1)
    def text(ids: list[int]) -> str:
        return "x"

    with pytest.raises(BatchError, match="got str"):
        text(1)


def test_sync_base_exception_is_raised_in_the_running_thread_too():
    class Stop(BaseException):
        pass

    @batch_calls(max_size=1)
    def load(ids: list[int]) -> list[int]:
        raise Stop

    with pytest.raises(Stop):
        load(1)


def test_sync_methods_batch_per_instance():
    seen = []

    class Repo:
        def __init__(self, name):
            self.name = name

        @batch_calls(max_size=1)
        def get(self, ids: list[int]) -> list[str]:
            seen.append(self.name)
            return [f"{self.name}{i}" for i in ids]

        @classmethod
        @batch_calls(max_size=1)
        def cget(cls, ids: list[int]) -> list[str]:
            return [cls.__name__ for _ in ids]

        @staticmethod
        @batch_calls(max_size=1)
        def sget(ids: list[int]) -> list[int]:
            return ids

    assert Repo("a").get(1) == "a1"
    assert Repo("b").get(2) == "b2"
    assert Repo.cget(0) == "Repo"
    assert Repo("c").cget(0) == "Repo"
    assert Repo.sget(3) == 3
    assert Repo("d").sget(4) == 4
    assert seen == ["a", "b"]
    assert Repo("e").get.pending == 0


def test_sync_call_needs_exactly_one_item():
    @batch_calls()
    def load(ids: list[int]) -> list[int]:
        return ids

    with pytest.raises(TypeError, match="one item"):
        load()
    with pytest.raises(TypeError):
        load(item=1)
    with pytest.raises(TypeError, match="not hashable"):
        load([1])
