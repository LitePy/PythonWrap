"""Durable buffered writers: SQLite and Redis stream journals with at-least-once replay."""

from __future__ import annotations

import asyncio
import os
import pickle
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from python_wrap import AsyncBuffered, Buffered, buffered
from python_wrap.buffering import BufferJournal, RedisStreamJournal, SQLiteJournal

ROOT = Path(__file__).resolve().parent.parent


def test_sqlite_journal_roundtrip_and_validation(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "j.db")
    assert isinstance(journal, BufferJournal)
    first = journal.append({"id": 1})
    second = journal.append([1, 2])
    assert journal.pending() == [(first, {"id": 1}), (second, [1, 2])]
    other = SQLiteJournal(tmp_path / "j.db", name="other")
    assert other.pending() == []
    journal.ack([first])
    journal.ack([])
    assert journal.pending() == [(second, [1, 2])]
    journal.close()
    journal.close()
    reopened = SQLiteJournal(tmp_path / "j.db")
    assert reopened.pending() == [(second, [1, 2])]
    reopened.close()
    other.close()
    for bad in ("", ":memory:", "file::memory:?cache=shared"):
        with pytest.raises(ValueError):
            SQLiteJournal(bad)
    with pytest.raises(ValueError):
        SQLiteJournal(tmp_path / "x.db", name="")
    with pytest.raises(ValueError):
        SQLiteJournal(tmp_path / "x.db", busy_timeout=-1)


def test_sqlite_journal_custom_codec_and_bad_rows(tmp_path: Path, caplog) -> None:
    journal = SQLiteJournal(tmp_path / "p.db", dumps=pickle.dumps, loads=pickle.loads)
    journal.append((1, "tuple"))
    journal.ack(list(range(10_000, 10_700)))  # chunked deletes of unknown ids are harmless
    assert [record for _, record in journal.pending()] == [(1, "tuple")]
    plain = SQLiteJournal(tmp_path / "p.db")  # JSON cannot read the pickled row
    assert plain.pending() == []
    assert "undecodable journal record" in caplog.text
    journal.close()
    plain.close()


def test_buffered_writes_and_acks(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "out.db")
    written: list[Any] = []

    @buffered(max_size=2, flush_interval=0.01, journal=journal)
    def write(rows: list[Any]) -> None:
        written.extend(rows)

    assert isinstance(write, Buffered) and write.journal is journal
    assert write.submit({"n": 1}) and write.submit({"n": 2}) and write.submit({"n": 3})
    assert write.close(timeout=2)
    assert written == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert journal.pending() == []
    journal.close()


def test_failed_batches_stay_journaled_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "out.db"
    journal = SQLiteJournal(path)

    def fail(rows: list[Any]) -> None:
        raise ConnectionError("database down")

    writer = Buffered(fail, flush_interval=0.001, journal=journal)
    assert writer.submit("a") and writer.submit("b")
    assert writer.close(timeout=2)
    assert writer.stats().failed_records == 2
    assert [record for _, record in journal.pending()] == ["a", "b"]
    journal.close()

    # "Restart": a new writer on the same journal replays the backlog first.
    restarted = SQLiteJournal(path)
    written: list[Any] = []
    writer2 = Buffered(written.extend, flush_interval=0.001, journal=restarted)
    assert writer2.replay() == 2
    assert writer2.replay() == 0  # only once
    assert writer2.submit("c")
    assert writer2.close(timeout=2)
    assert written == ["a", "b", "c"]
    assert restarted.pending() == []
    restarted.close()


def test_replay_happens_on_first_submit(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "out.db")
    journal.append("old")
    written: list[Any] = []
    writer = Buffered(written.extend, flush_interval=0.001, journal=journal)
    assert writer.submit("new")
    assert writer.flush(timeout=2)
    assert written == ["old", "new"]
    assert writer.close(timeout=2)
    assert Buffered(written.extend).replay() == 0  # no journal
    journal.close()


def test_drop_oldest_acks_the_dropped_record(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "out.db")
    started, release = threading.Event(), threading.Event()
    written: list[Any] = []

    def handler(rows: list[Any]) -> None:
        started.set()
        release.wait(2)
        written.extend(rows)

    writer = Buffered(
        handler, max_size=1, max_queue=1, flush_interval=0.001, on_full="drop_oldest",
        journal=journal,
    )  # fmt: skip
    assert writer.submit(0)
    assert started.wait(2)
    assert writer.submit(1) and writer.submit(2)  # 1 is dropped on purpose
    assert sorted(r for _, r in journal.pending()) == [0, 2]
    release.set()
    assert writer.close(timeout=2)
    assert written == [0, 2]
    assert journal.pending() == []
    journal.close()


def test_journal_errors(tmp_path: Path, caplog) -> None:
    class Broken:
        def append(self, record: Any) -> int:
            raise OSError("disk full")

        def ack(self, ids: Any) -> None:
            raise OSError("disk full")

        def pending(self) -> list[Any]:
            return []

    writer = Buffered(lambda rows: None, flush_interval=0.001, journal=Broken())
    with pytest.raises(OSError):
        writer.submit(1)
    assert writer.stats().queued == 0
    assert writer.close(timeout=2)

    class AckFails(Broken):
        def append(self, record: Any) -> int:
            return 1

    writer = Buffered(lambda rows: None, flush_interval=0.001, journal=AckFails())
    assert writer.submit(1)
    assert writer.close(timeout=2)
    assert writer.stats().written == 1
    assert "journal ack failed" in caplog.text

    with pytest.raises(TypeError, match="BufferJournal"):
        Buffered(lambda rows: None, journal=object())  # type: ignore[arg-type]


def test_buffered_methods_reject_journal(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "out.db")

    class Repo:
        @buffered(journal=journal)
        def write(self, rows: list[Any]) -> None: ...

    with pytest.raises(TypeError, match="methods"):
        _ = Repo().write
    journal.close()


def test_async_buffered_journal_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "async.db"
    journal = SQLiteJournal(path)
    journal.append({"left": "over"})
    written: list[Any] = []

    async def main() -> None:
        @buffered(max_size=10, flush_interval=0.01, journal=journal)
        async def write(rows: list[Any]) -> None:
            written.extend(rows)

        assert isinstance(write, AsyncBuffered)
        assert await write.replay() == 1
        assert await write.replay() == 0
        assert await write.submit({"n": 1})
        assert await write.close(timeout=2)

    asyncio.run(main())
    assert written == [{"left": "over"}, {"n": 1}]
    assert journal.pending() == []

    async def failing() -> None:
        async def fail(rows: list[Any]) -> None:
            raise RuntimeError("down")

        writer = AsyncBuffered(fail, flush_interval=0.001, journal=journal, on_full="drop_oldest",
                               max_queue=1)  # fmt: skip
        assert await writer.submit("x")
        assert await writer.close(timeout=2)

    asyncio.run(failing())
    assert [r for _, r in journal.pending()] == ["x"]

    async def no_journal() -> None:
        writer = AsyncBuffered(written.extend)
        assert await writer.replay() == 0

    asyncio.run(no_journal())
    journal.close()


def test_async_drop_oldest_acks(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "async.db")

    async def main() -> None:
        gate = asyncio.Event()

        async def slow(rows: list[Any]) -> None:
            await gate.wait()

        writer = AsyncBuffered(slow, max_size=1, max_queue=1, flush_interval=0.001,
                               on_full="drop_oldest", journal=journal)  # fmt: skip
        assert await writer.submit(0)
        await asyncio.sleep(0.01)
        assert await writer.submit(1) and await writer.submit(2)
        assert sorted(r for _, r in journal.pending()) == [0, 2]
        gate.set()
        assert await writer.close(timeout=2)

    asyncio.run(main())
    assert journal.pending() == []
    journal.close()


CRASH_SCRIPT = """
import os, sys
from python_wrap.buffering import Buffered, SQLiteJournal

journal = SQLiteJournal(sys.argv[1])
writer = Buffered(lambda rows: None, flush_interval=60, max_size=1000, journal=journal)
for n in range(5):
    assert writer.submit({"n": n})
os._exit(1)  # crash before the writer flushes anything
"""


def test_records_survive_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(path)], env=env, timeout=30, check=False
    )
    assert result.returncode == 1
    written: list[Any] = []
    journal = SQLiteJournal(path)
    writer = Buffered(written.extend, flush_interval=0.001, journal=journal)
    assert writer.replay() == 5
    assert writer.close(timeout=2)
    assert written == [{"n": n} for n in range(5)]
    journal.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork only")
def test_fork_child_does_not_replay(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "fork.db")
    journal.append("parent-backlog")
    writer = Buffered(lambda rows: None, flush_interval=0.001, journal=journal)
    read, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            replayed = writer._replayed
            os.write(write_end, b"1" if replayed else b"0")
        finally:
            os._exit(0)
    os.close(write_end)
    assert os.read(read, 1) == b"1"
    os.close(read)
    os.waitpid(pid, 0)
    assert writer.replay() == 1
    assert writer.close(timeout=2)
    journal.close()


class FakeStreams:
    """Enough of redis.Redis for XADD/XDEL/XRANGE, with bytes like the real client."""

    def __init__(self) -> None:
        self.entries: list[tuple[bytes, dict[bytes, bytes]]] = []
        self.counter = 0
        self.pages = 0

    def xadd(self, stream: str, fields: dict[str, bytes]) -> bytes:
        self.counter += 1
        entry_id = f"1-{self.counter}".encode()
        self.entries.append((entry_id, {k.encode(): v for k, v in fields.items()}))
        return entry_id

    def xdel(self, stream: str, *ids: bytes) -> int:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e[0] not in ids]
        return before - len(self.entries)

    def xrange(self, stream: str, min: str, max: str, count: int) -> list[Any]:
        self.pages += 1
        after = 0 if min == "-" else int(min.lstrip("(").split("-")[1])
        rows = [e for e in self.entries if int(e[0].split(b"-")[1]) > after]
        return rows[:count]


def test_redis_stream_journal_with_fake_client() -> None:
    client = FakeStreams()
    journal = RedisStreamJournal(client, "outbox")
    assert isinstance(journal, BufferJournal)
    for n in range(1203):
        journal.append({"n": n})
    pending = journal.pending()
    assert len(pending) == 1203 and client.pages == 3
    assert pending[0][1] == {"n": 0} and pending[-1][1] == {"n": 1202}

    written: list[Any] = []
    writer = Buffered(written.extend, max_size=500, flush_interval=0.001, journal=journal)
    assert writer.replay() == 1203
    assert writer.submit({"n": "new"})
    assert writer.close(timeout=5)
    assert len(written) == 1204 and client.entries == []

    client.entries.append((b"1-99999", {b"r": b"not json"}))
    assert journal.pending() == []
    with pytest.raises(ValueError):
        RedisStreamJournal(client, "")


def test_redis_stream_journal_string_responses() -> None:
    class Decoded(FakeStreams):
        def xrange(self, stream: str, min: str, max: str, count: int) -> list[Any]:
            return [
                (i.decode(), {k.decode(): v for k, v in f.items()})
                for i, f in super().xrange(stream, min, max, count)
            ]

    journal = RedisStreamJournal(Decoded(), "outbox")
    journal.append([1])
    assert [r for _, r in journal.pending()] == [[1]]
