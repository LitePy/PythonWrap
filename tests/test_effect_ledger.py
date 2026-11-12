"""EffectLedger: call-id idempotency, started/succeeded/failed states, crash detection."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from python_wrap import (
    EffectConflict,
    EffectLedger,
    EffectRecord,
    EffectResolved,
    EffectUnknown,
    MemoryEffectStore,
    RedisEffectStore,
    SQLiteEffectStore,
    TimeoutExpired,
    ToolRegistry,
    agent_run,
    tool,
    tool_call_id,
    with_call_id,
)
from python_wrap.events import listening
from python_wrap.stores import RedisStore

# ------------------------------------------------------------------ fakes


class FakeRedis:
    """A dict-backed client that runs the ledger's two Lua scripts in Python."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def eval(self, script: str, numkeys: int, key: str, *argv: Any) -> Any:
        from python_wrap.agents import _REDIS_CLAIM, _REDIS_SETTLE

        with self.lock:
            entry = self.data.get(key)
            if script == _REDIS_CLAIM:
                expect, owner, data, _ttl = argv
                if entry is None or (expect != "" and entry["owner"] == expect):
                    self.data[key] = {"owner": owner, "data": data}
                    return None
                return entry["data"]
            assert script == _REDIS_SETTLE
            owner, data, _ttl = argv
            if entry is not None and entry["owner"] == owner:
                entry["data"] = data
                return 1
            return 0

    def hget(self, key: str, field: str) -> Any:
        with self.lock:
            entry = self.data.get(key)
            return None if entry is None else entry[field]

    def delete(self, key: str) -> int:
        with self.lock:
            return 1 if self.data.pop(key, None) is not None else 0


class FlakyStore(MemoryEffectStore):
    """Fails `settle` on demand: the crash between the side effect and the record."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_settle = 0

    def settle(self, record: EffectRecord, ttl: float, /) -> bool:
        if self.fail_settle:
            self.fail_settle -= 1
            raise ConnectionError("store went away")
        return super().settle(record, ttl)


def stores(tmp_path: Path) -> dict[str, Any]:
    return {
        "memory": MemoryEffectStore(),
        "sqlite": SQLiteEffectStore(tmp_path / "effects.db"),
        "redis": RedisEffectStore(FakeRedis()),
    }


@pytest.fixture(params=["memory", "sqlite", "redis"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    return stores(tmp_path)[request.param]


# ------------------------------------------------------------------ basics


def test_call_id_replay_returns_stored_result_new_id_runs(store: Any) -> None:
    ledger = EffectLedger(store)
    sent: list[str] = []

    @tool(ledger=ledger)
    def send(to: str) -> str:
        sent.append(to)
        return f"sent {to} #{len(sent)}"

    with agent_run():
        with with_call_id("call_1"):
            first = send("a@example.com")
            again = send("a@example.com")
        with with_call_id("call_2"):
            third = send("a@example.com")
    assert first == again == "sent a@example.com #1"
    assert third == "sent a@example.com #2"
    assert sent == ["a@example.com", "a@example.com"]
    record = ledger.lookup("send:call:call_1")
    assert record is not None and record.state == "succeeded" and record.result == first


def test_without_call_id_the_args_digest_keys_within_a_run(store: Any) -> None:
    ledger = EffectLedger(store)
    calls: list[int] = []

    @tool(ledger=ledger)
    def bump(n: int) -> int:
        calls.append(n)
        return n + 1

    with agent_run(run_id="r1"):
        assert bump(1) == bump(n=1) == 2
        assert bump(2) == 3
    with agent_run(run_id="r2"):
        bump(1)
    assert calls == [1, 2, 1]


def test_call_id_reused_with_other_arguments_is_a_conflict() -> None:
    ledger = EffectLedger()

    @tool(ledger=ledger)
    def pay(amount: int) -> int:
        return amount

    with with_call_id("call_x"):
        pay(5)
        with pytest.raises(EffectConflict) as info:
            pay(6)
    assert info.value.record.call_id == "call_x"


def test_business_key_wins_over_call_id() -> None:
    ledger = EffectLedger()
    runs: list[str] = []

    @tool(ledger=ledger, idempotent_key=lambda order, cents: order)
    def charge(order: str, cents: int) -> str:
        runs.append(order)
        return "ok"

    with with_call_id("c1"):
        charge("A", 1)
    with with_call_id("c2"):
        charge("A", 1)
    assert runs == ["A"]


def test_failed_call_records_failed_and_next_call_runs_again(store: Any) -> None:
    ledger = EffectLedger(store)
    attempts: list[int] = []

    @tool(ledger=ledger)
    def flaky() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("bad input")
        return "ok"

    with with_call_id("c1"):
        with pytest.raises(ValueError):
            flaky()
        record = ledger.lookup("flaky:call:c1")
        assert record is not None and record.state == "failed"
        assert record.error == {"type": "ValueError", "message": "bad input"}
        assert flaky() == "ok"
    final = ledger.lookup("flaky:call:c1")
    assert final is not None and final.state == "succeeded" and final.attempts == 2


def test_read_only_tool_with_ledger_is_deduplicated_unless_disabled() -> None:
    ledger = EffectLedger()
    runs: list[int] = []

    @tool(read_only=True, ledger=ledger)
    def look() -> int:
        runs.append(1)
        return len(runs)

    @tool(read_only=True, ledger=ledger, idempotent_key=False)
    def look_live() -> int:
        runs.append(2)
        return len(runs)

    with with_call_id("c1"):
        look()
        look()
        look_live()
        look_live()
    assert runs == [1, 2, 2]


def test_registry_call_passes_call_id() -> None:
    ledger = EffectLedger()
    tools = ToolRegistry()
    runs: list[str] = []

    @tools.tool(ledger=ledger)
    def note(text: str) -> str:
        runs.append(text)
        return text

    with tools.run(), with_call_id("toolu_1"):
        tools.call("note", '{"text": "hi"}')
        tools.call("note", {"text": "hi"})
    assert runs == ["hi"]


def test_tool_call_carries_call_id_to_approver() -> None:
    from python_wrap import ToolCall, ToolPolicy

    seen: list[ToolCall] = []

    def approve(call: ToolCall) -> bool:
        seen.append(call)
        return True

    @tool(requires_approval=True, ledger=EffectLedger())
    def wipe(path: str) -> str:
        return path

    with agent_run(policy=ToolPolicy(approver=approve)), with_call_id("call_9"):
        wipe("/tmp/x")
    assert seen[0].call_id == "call_9"


def test_with_call_id_validates_and_resets() -> None:
    with pytest.raises(ValueError), with_call_id(""):
        pass
    with with_call_id("a"), with_call_id(None):
        assert tool_call_id.get() is None
    assert tool_call_id.get() is None


def test_ledger_argument_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EffectLedger(ttl=0)
    with pytest.raises(ValueError):
        EffectLedger(ttl=10, lease=20)
    with pytest.raises(TypeError):
        EffectLedger(on_unknown="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EffectLedger(poll=0)
    with pytest.raises(TypeError):
        EffectLedger(store=42)
    with pytest.raises(TypeError):
        tool(ledger="x")(lambda: 1)  # type: ignore[call-overload]
    with pytest.raises(ValueError):
        MemoryEffectStore(max_entries=0)
    assert isinstance(EffectLedger(str(tmp_path / "x.db")).store, SQLiteEffectStore)
    assert "EffectLedger(MemoryEffectStore" in repr(EffectLedger())


def test_redis_store_reuses_redis_store_settings() -> None:
    client = FakeRedis()
    with pytest.raises(ValueError):
        EffectLedger(RedisStore(client, "pfx:"))  # fail_open=True by default
    ledger = EffectLedger(RedisStore(client, "pfx:", fail_open=False))
    assert isinstance(ledger.store, RedisEffectStore)
    assert ledger.store.prefix == "pfx:effects:"

    @tool(ledger=ledger)
    def act() -> dict[str, int]:
        return {"n": 1}

    with with_call_id("c"):
        assert act() == act() == {"n": 1}
    assert any(key.startswith("pfx:effects:") for key in client.data)
    with pytest.raises(ValueError):
        RedisEffectStore(client, "")


def test_forget_lets_the_call_run_again() -> None:
    ledger = EffectLedger()
    runs: list[int] = []

    @tool(ledger=ledger)
    def act() -> int:
        runs.append(1)
        return len(runs)

    with with_call_id("c"):
        act()
        assert ledger.forget("act:call:c")
        assert act() == 2
    assert ledger.key("act", call_id="c") == "act:call:c"
    assert ledger.key("act", arguments={"a": 1}, run_id="r").startswith("act:r:")
    assert ledger.key("act", business_key="B-1") == "act:key:B-1"


def test_events_report_replay() -> None:
    ledger = EffectLedger()

    @tool(ledger=ledger)
    def act() -> int:
        return 1

    events: list[Any] = []
    with listening(events.append), with_call_id("c"):
        act()
        act()
    names = [event.name for event in events]
    assert names.count("tool.call") == 1
    assert "tool.replayed" in names


# ------------------------------------------------------------------ crash detection


def test_store_failure_after_effect_is_detected_as_unknown() -> None:
    store = FlakyStore()
    ledger = EffectLedger(store)
    effects: list[str] = []

    @tool(ledger=ledger)
    def transfer(cents: int) -> str:
        effects.append("moved")
        return "done"

    with with_call_id("c1"):
        store.fail_settle = 1
        with pytest.raises(ConnectionError):
            transfer(100)
        assert effects == ["moved"]
        with pytest.raises(EffectUnknown) as info:
            transfer(100)
    assert info.value.reason == "crashed"
    assert info.value.record.state == "started"
    assert effects == ["moved"]  # never re-run blindly


def test_unknown_hook_can_resolve_with_the_looked_up_outcome() -> None:
    store = FlakyStore()
    seen: list[EffectUnknown] = []

    def decide(error: EffectUnknown) -> Any:
        seen.append(error)
        return EffectResolved("done (reconciled)")

    ledger = EffectLedger(store, on_unknown=decide)
    effects: list[str] = []

    @tool(ledger=ledger)
    def transfer(cents: int) -> str:
        effects.append("moved")
        return "done"

    with with_call_id("c1"):
        store.fail_settle = 1
        with pytest.raises(ConnectionError):
            transfer(1)
        assert transfer(1) == "done (reconciled)"
        assert transfer(1) == "done (reconciled)"  # now a normal replay
    assert effects == ["moved"]
    assert len(seen) == 1


def test_unknown_hook_retry_runs_again() -> None:
    store = FlakyStore()
    ledger = EffectLedger(store, on_unknown=lambda error: "retry")
    effects: list[str] = []

    @tool(ledger=ledger)
    def transfer() -> str:
        effects.append("moved")
        return f"done {len(effects)}"

    with with_call_id("c1"):
        store.fail_settle = 1
        with pytest.raises(ConnectionError):
            transfer()
        assert transfer() == "done 2"
    record = ledger.lookup("transfer:call:c1")
    assert record is not None and record.attempts == 2


def test_unknown_hook_raise_and_bad_answer() -> None:
    for answer, error in (("raise", EffectUnknown), (None, EffectUnknown), (3, TypeError)):
        store = FlakyStore()
        ledger = EffectLedger(store, on_unknown=lambda e, a=answer: a)

        @tool(ledger=ledger)
        def act() -> int:
            return 1

        with with_call_id("c"):
            store.fail_settle = 1
            with pytest.raises(ConnectionError):
                act()
            with pytest.raises(error):
                act()


def test_started_record_from_a_dead_process_is_unknown_after_the_lease(
    tmp_path: Path, fake_clock: Any
) -> None:
    path = tmp_path / "effects.db"
    # Process A claimed the call and died before recording.
    crashed = EffectLedger(SQLiteEffectStore(path), lease=30)
    mine = crashed._new("act:call:c1", "act", None, "c1", 1)
    assert crashed.store.claim(mine, crashed.ttl, None) is None
    # Process B replays the run.
    ledger = EffectLedger(SQLiteEffectStore(path), lease=30, wait_timeout=None)
    runs: list[int] = []

    @tool(ledger=ledger)
    def act() -> int:
        runs.append(1)
        return 1

    fake_clock.advance(31)
    with with_call_id("c1"), pytest.raises(EffectUnknown) as info:
        act()
    assert info.value.reason == "crashed"
    assert runs == []


def test_duplicate_waits_for_in_flight_owner_elsewhere_then_replays(
    tmp_path: Path,
) -> None:
    path = tmp_path / "effects.db"
    other = EffectLedger(SQLiteEffectStore(path))
    mine = other._new("act:call:c1", "act", None, "c1", 1)
    other.store.claim(mine, other.ttl, None)
    ledger = EffectLedger(SQLiteEffectStore(path), poll=0.01)

    def finish_later() -> None:
        time.sleep(0.1)
        other._settle(mine, 41, None)

    threading.Thread(target=finish_later).start()

    @tool(ledger=ledger)
    def act() -> int:
        raise AssertionError("must not run")

    with with_call_id("c1"):
        assert act() == 41


def test_wait_timeout_raises_timeout_expired(tmp_path: Path) -> None:
    path = tmp_path / "effects.db"
    other = EffectLedger(SQLiteEffectStore(path))
    other.store.claim(other._new("act:call:c1", "act", None, "c1", 1), other.ttl, None)
    ledger = EffectLedger(SQLiteEffectStore(path), wait_timeout=0.05, poll=0.01)

    @tool(ledger=ledger)
    def act() -> int:
        return 1

    with with_call_id("c1"), pytest.raises(TimeoutExpired):
        act()


def test_timeout_failure_is_uncertain() -> None:
    ledger = EffectLedger()
    runs: list[int] = []

    @tool(ledger=ledger)
    def slow() -> int:
        runs.append(1)
        raise TimeoutError("upstream did not answer")

    with with_call_id("c1"):
        with pytest.raises(TimeoutError):
            slow()
        with pytest.raises(EffectUnknown) as info:
            slow()
    assert info.value.reason == "uncertain"
    assert runs == [1]


def test_model_retry_is_never_uncertain() -> None:
    class ModelRetry(TimeoutError):  # a look-alike of pydantic-ai's ModelRetry
        pass

    ledger = EffectLedger()
    runs: list[int] = []

    @tool(ledger=ledger)
    def ask() -> int:
        runs.append(1)
        if len(runs) == 1:
            raise ModelRetry("try again")
        return 2

    with with_call_id("c1"):
        with pytest.raises(ModelRetry):
            ask()
        assert ask() == 2


def test_unserialisable_result_is_result_lost_on_replay(tmp_path: Path) -> None:
    ledger = EffectLedger(SQLiteEffectStore(tmp_path / "e.db"))
    runs: list[int] = []

    @tool(ledger=ledger)
    def make() -> object:
        runs.append(1)
        return object()

    with with_call_id("c1"):
        make()
        with pytest.raises(EffectUnknown) as info:
            make()
    assert info.value.reason == "result_lost"
    assert runs == [1]


def test_record_round_trips_through_dict() -> None:
    record = EffectRecord(key="k", tool="t", state="succeeded", owner="o", result=[1])
    assert EffectRecord.from_dict({**record.to_dict(), "extra": 1}) == record


def test_stale_owner_cannot_overwrite_a_taken_over_record(caplog: Any) -> None:
    store = MemoryEffectStore()
    ledger = EffectLedger(store)
    first = ledger._new("k", "t", None, None, 1)
    store.claim(first, 60, None)
    second = ledger._new("k", "t", None, None, 2)
    assert store.claim(second, 60, first.owner) is None
    ledger._settle(first, "stale", None)
    current = store.get("k")
    assert current is not None and current.owner == second.owner
    assert "taken over" in caplog.text


def test_memory_store_is_bounded_and_expires(fake_clock: Any) -> None:
    store = MemoryEffectStore(max_entries=2)
    for key in "abc":
        store.claim(EffectRecord(key=key, tool="t", state="started", owner=key), 10, None)
    assert len(store) == 2 and store.get("a") is None
    fake_clock.advance(11)
    assert store.get("c") is None
    assert not store.delete("zz")


# ------------------------------------------------------------------ async


async def test_async_tool_replay_and_failure(store: Any) -> None:
    ledger = EffectLedger(store)
    runs: list[str] = []

    @tool(ledger=ledger)
    async def send(to: str) -> str:
        runs.append(to)
        await asyncio.sleep(0)
        return f"sent {to}"

    with with_call_id("c1"):
        assert await send("x") == await send("x") == "sent x"
    assert runs == ["x"]


async def test_async_crash_then_resolve_with_async_hook() -> None:
    store = FlakyStore()

    async def decide(error: EffectUnknown) -> Any:
        await asyncio.sleep(0)
        return EffectResolved("reconciled")

    ledger = EffectLedger(store, on_unknown=decide)
    runs: list[int] = []

    @tool(ledger=ledger)
    async def act() -> str:
        runs.append(1)
        return "real"

    with with_call_id("c1"):
        store.fail_settle = 1
        with pytest.raises(ConnectionError):
            await act()
        assert await act() == "reconciled"
    assert runs == [1]


async def test_async_cancellation_is_uncertain() -> None:
    ledger = EffectLedger()
    started = asyncio.Event()

    @tool(ledger=ledger)
    async def slow() -> int:
        started.set()
        await asyncio.sleep(10)
        return 1

    async def call() -> int:
        with with_call_id("c1"):
            return await slow()

    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with with_call_id("c1"), pytest.raises(EffectUnknown) as info:
        await slow()
    assert info.value.reason == "uncertain"


async def test_async_retry_hook_and_wait_timeout(tmp_path: Path) -> None:
    path = tmp_path / "effects.db"
    other = EffectLedger(SQLiteEffectStore(path))
    other.store.claim(other._new("act:call:c1", "act", None, "c1", 1), other.ttl, None)
    ledger = EffectLedger(SQLiteEffectStore(path), wait_timeout=0.05, poll=0.01)

    @tool(ledger=ledger)
    async def act() -> int:
        return 1

    with with_call_id("c1"), pytest.raises(TimeoutExpired):
        await act()


# ------------------------------------------------------------------ concurrency


def _count_runs_threads(ledger: EffectLedger, workers: int, call_id: str) -> tuple[int, set[Any]]:
    runs: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    @tool(ledger=ledger)
    def effect(order: str) -> str:
        with lock:
            runs.append(1)
        time.sleep(0.01)
        return f"done {order}"

    results: list[Any] = []

    def worker() -> None:
        barrier.wait()
        with with_call_id(call_id):
            results.append(effect("A-1"))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(runs), set(results)


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(workers=st.integers(min_value=2, max_value=12), call_id=st.text(min_size=1, max_size=12))
def test_property_concurrent_same_call_id_runs_exactly_once(workers: int, call_id: str) -> None:
    runs, results = _count_runs_threads(EffectLedger(poll=0.005), workers, call_id)
    assert runs == 1
    assert results == {"done A-1"}


def test_concurrent_same_call_id_sqlite_runs_once(tmp_path: Path) -> None:
    ledger = EffectLedger(SQLiteEffectStore(tmp_path / "e.db"), poll=0.005)
    runs, results = _count_runs_threads(ledger, 8, "call_1")
    assert runs == 1 and results == {"done A-1"}


def test_concurrent_same_call_id_two_ledgers_one_redis(tmp_path: Path) -> None:
    client = FakeRedis()
    ledgers = [EffectLedger(RedisEffectStore(client), poll=0.005) for _ in range(2)]
    runs: list[int] = []
    barrier = threading.Barrier(6)

    def make(ledger: EffectLedger) -> Callable[[], str]:
        @tool(ledger=ledger, name="effect")
        def effect() -> str:
            runs.append(1)
            time.sleep(0.02)
            return "ok"

        return effect

    tools = [make(ledger) for ledger in ledgers]
    results: list[str] = []

    def worker(index: int) -> None:
        barrier.wait()
        with with_call_id("shared"):
            results.append(tools[index % 2]())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(runs) == 1 and results == ["ok"] * 6


async def test_concurrent_same_call_id_asyncio_runs_once() -> None:
    ledger = EffectLedger(poll=0.005)
    runs: list[int] = []

    @tool(ledger=ledger)
    async def effect() -> str:
        runs.append(1)
        await asyncio.sleep(0.02)
        return "ok"

    async def one() -> str:
        with with_call_id("c1"):
            return await effect()

    results = await asyncio.gather(*(one() for _ in range(10)))
    assert runs == [1] and set(results) == {"ok"}


@settings(max_examples=25, deadline=None)
@given(plan=st.lists(st.tuples(st.sampled_from(["c1", "c2", "c3"]), st.booleans()), max_size=12))
def test_property_crash_injection_never_runs_an_effect_twice_blindly(
    plan: list[tuple[str, bool]],
) -> None:
    """Whatever mix of store failures happens, a call id's effect runs at most once
    unless the hook explicitly says retry (it never does here)."""
    store = FlakyStore()
    ledger = EffectLedger(store)
    effects: dict[str, int] = {}

    @tool(ledger=ledger)
    def act() -> str:
        call = tool_call_id.get()
        assert call is not None
        effects[call] = effects.get(call, 0) + 1
        return call

    for call_id, crash in plan:
        store.fail_settle = 1 if crash else 0
        with with_call_id(call_id), contextlib.suppress(ConnectionError, EffectUnknown):
            assert act() == call_id
    assert all(count == 1 for count in effects.values())


async def test_sync_tool_with_async_hook_inside_a_running_loop() -> None:
    store = FlakyStore()

    async def decide(error: EffectUnknown) -> Any:
        return EffectResolved("found")

    @tool(ledger=EffectLedger(store, on_unknown=decide))
    def act() -> str:
        return "real"

    with with_call_id("c1"):
        store.fail_settle = 1
        with pytest.raises(ConnectionError):
            act()
        assert act() == "found"  # sync tool called while this event loop runs
