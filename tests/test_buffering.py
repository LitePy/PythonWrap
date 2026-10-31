from __future__ import annotations

import threading

import pytest

from python_wrap import AsyncBuffered, Buffered, buffered


def test_buffered_batches_flush_close_and_failure():
    batches = []
    writer = Buffered(lambda rows: batches.append(rows), max_size=2, flush_interval=0.01)
    assert writer.submit(1) and writer.submit(2) and writer.submit(3)
    assert writer.close(timeout=2)
    assert batches == [[1, 2], [3]]
    assert writer.stats().written == 3 and writer.stats().queued == 0
    assert not writer.submit(4)

    def fail(rows):
        raise ValueError("write failed")

    writer = Buffered(fail, flush_interval=0.001)
    writer.submit(1)
    assert writer.close(timeout=2)
    assert writer.stats().failed_batches == 1 and writer.stats().failed_records == 1


@pytest.mark.parametrize("mode,expected", [("drop", [0, 1]), ("drop_oldest", [0, 2])])
def test_bounded_buffer_drop_policies(mode, expected):
    started, release = threading.Event(), threading.Event()
    written = []

    def write(rows):
        started.set()
        assert release.wait(2)
        written.extend(rows)

    writer = Buffered(write, max_size=1, max_queue=1, on_full=mode)
    writer.submit(0)
    assert started.wait(2)
    writer.submit(1)
    accepted = writer.submit(2)
    assert accepted is (mode == "drop_oldest")
    release.set()
    assert writer.close(2)
    assert written == expected
    assert writer.stats().dropped == 1


def test_blocked_submit_timeout_and_disable():
    started, release = threading.Event(), threading.Event()

    def write(rows):
        started.set()
        assert release.wait(2)

    writer = Buffered(write, max_size=1, max_queue=1, on_full="block")
    writer.submit(0)
    assert started.wait(2)
    writer.submit(1)
    assert not writer.submit(2, timeout=0)
    writer.disable()
    assert not writer.submit(3)
    release.set()
    assert writer.close(2)


@pytest.mark.asyncio
async def test_async_buffer_batches_lifecycle_and_failures():
    batches = []

    @buffered(max_size=2, flush_interval=0.01)
    async def write(rows):
        batches.append(rows)

    assert isinstance(write, AsyncBuffered)
    async with write:
        assert await write.submit(1)
        assert await write.submit(2)
        assert await write.submit(3)
        assert await write.flush(2)
    assert batches == [[1, 2], [3]]
    assert write.stats().written == 3
    assert not await write.submit(4)


@pytest.mark.asyncio
async def test_async_handler_failure_disables_and_counts():
    async def bad(rows):
        raise ValueError("write")

    writer = AsyncBuffered(bad, max_size=1, on_error="disable")
    await writer.submit(1)
    await writer.submit(2)
    assert await writer.flush(1)
    assert writer.stats().failed_batches == 1
    assert writer.stats().dropped == 1
    assert not await writer.submit(3)
    assert await writer.close()


def test_flush_interrupts_long_interval_and_timeout_restores_normal_batching():
    batches = []
    writer = Buffered(lambda rows: batches.append(rows), flush_interval=60)
    writer.submit(1)
    assert writer.flush(0.5)
    assert batches == [[1]]
    assert writer.close(0.5)
    assert not writer._thread.is_alive()


@pytest.mark.asyncio
async def test_async_flush_interrupts_long_interval():
    batches = []
    writer = AsyncBuffered(lambda rows: None, flush_interval=60)

    async def save(rows):
        batches.append(rows)

    writer.func = save
    await writer.submit(1)
    assert await writer.flush(0.5)
    assert batches == [[1]]
    assert await writer.close(0.5)
    assert writer._task.done()


@pytest.mark.asyncio
async def test_async_cancelled_submit_is_never_queued_and_close_rejects_waiters():
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    rows = []

    async def write(batch):
        started.set()
        await release.wait()
        rows.extend(batch)

    writer = AsyncBuffered(write, max_size=1, max_queue=1, on_full="block")
    await writer.submit(0)
    await started.wait()
    await writer.submit(1)
    waiting = asyncio.create_task(writer.submit(2))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    waiting = asyncio.create_task(writer.submit(3))
    await asyncio.sleep(0)
    closing = asyncio.create_task(writer.close(1))
    assert await asyncio.wait_for(waiting, 0.5) is False
    release.set()
    assert await closing
    assert rows == [0, 1]
    assert writer.stats().queued == 0


@pytest.mark.asyncio
async def test_cancelling_close_does_not_cancel_writer_or_lose_records():
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    written = []

    async def write(rows):
        started.set()
        await release.wait()
        written.extend(rows)

    writer = AsyncBuffered(write, max_size=1)
    await writer.submit(1)
    await started.wait()
    closing = asyncio.create_task(writer.close(None))
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not writer._task.cancelled()
    release.set()
    assert await writer.close(1)
    assert written == [1]


@pytest.mark.asyncio
async def test_cancelled_handler_disables_and_accounts_for_all_records():
    import asyncio

    started = asyncio.Event()

    async def write(rows):
        started.set()
        await asyncio.Event().wait()

    writer = AsyncBuffered(write, max_size=1)
    await writer.submit(1)
    await writer.submit(2)
    await started.wait()
    writer._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer._task
    assert await writer.flush(0.1)
    assert not await writer.submit(3)
    stats = writer.stats()
    assert stats.queued == 0 and stats.failed_records == 1 and stats.dropped == 2


def test_buffered_methods_have_per_instance_queues():
    class Writer:
        def __init__(self):
            self.rows = []

        @buffered(flush_interval=60)
        def write(self, rows):
            self.rows.extend(rows)

    a, b = Writer(), Writer()
    assert a.write is a.write and a.write is not b.write
    assert a.write.submit(1) and b.write.submit(2)
    assert a.write.close(0.5) and b.write.close(0.5)
    assert a.rows == [1] and b.rows == [2]


@pytest.mark.asyncio
async def test_async_buffered_methods_have_per_instance_queues():
    class Writer:
        def __init__(self):
            self.rows = []

        @buffered(flush_interval=60)
        async def write(self, rows):
            self.rows.extend(rows)

    a, b = Writer(), Writer()
    assert await a.write.submit(1) and await b.write.submit(2)
    assert await a.write.close(0.5) and await b.write.close(0.5)
    assert a.rows == [1] and b.rows == [2]


@pytest.mark.asyncio
async def test_async_buffer_close_after_handler_cancelled():
    import asyncio

    async def write(rows):
        raise asyncio.CancelledError()

    writer = AsyncBuffered(write, max_size=1)
    await writer.submit(1)
    with pytest.raises(asyncio.CancelledError):
        await writer._task
    assert await writer.close(0.1)
    assert writer.stats().failed_records == 1


def test_logging_failure_does_not_kill_writer_or_wedge_flush():
    class BadLogger:
        def exception(self, *args, **kw):
            raise RuntimeError("logger failed")

        warning = exception

    def write(rows):
        raise ValueError("handler failed")

    writer = Buffered(write, max_size=1)
    writer.log = BadLogger()
    assert writer.submit(1) and writer.submit(2)
    assert writer.close(1)
    assert writer.stats().failed_records == 2


@pytest.mark.asyncio
async def test_async_logging_failure_does_not_kill_writer():
    class BadLogger:
        def exception(self, *args, **kw):
            raise RuntimeError("logger failed")

        warning = exception

    async def write(rows):
        raise ValueError("handler failed")

    writer = AsyncBuffered(write, max_size=1)
    writer.log = BadLogger()
    assert await writer.submit(1) and await writer.submit(2)
    assert await writer.close(1)
    assert writer.stats().failed_records == 2
