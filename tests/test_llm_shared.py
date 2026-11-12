"""Shared token windows: SQLite and Redis backends, header parsing, cross-process limits."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import python_wrap
from python_wrap import _core
from python_wrap.errors import RateLimitExceeded
from python_wrap.llm import Budget, token_rate_limit
from python_wrap.llm_shared import (
    AsyncRedisTokenWindow,
    RedisTokenWindow,
    SharedTokenWindow,
    SQLiteBudgetBackend,
    SQLiteTokenWindow,
    _step,
    parse_rate_limit_headers,
)
from python_wrap.llm_testing import RateLimitError, fake_llm

try:  # the module-scoped disposable redis-server fixture lives next to the other redis tests
    from test_redis_backends import Client, redis_client  # noqa: F401
except ImportError:  # pragma: no cover
    redis_client = None

RATE = 600 / 60.0


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "tokens.db")


# ------------------------------------------------------------------------ header parsing


class TestHeaders:
    def test_openai_style(self) -> None:
        hint = parse_rate_limit_headers(
            {"x-ratelimit-remaining-tokens": "1200", "x-ratelimit-reset-tokens": "6m0s"}
        )
        assert (hint.remaining_tokens, hint.reset_seconds, hint.retry_after) == (1200, 360, None)
        assert hint.block_seconds is None

    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("1s", 1), ("20ms", 0.02), ("1m30.5s", 90.5), ("2h", 7200), ("1d", 86400), ("7", 7)],
    )
    def test_durations(self, text: str, seconds: float) -> None:
        hint = parse_rate_limit_headers({"x-ratelimit-reset-tokens": text})
        assert hint.reset_seconds == pytest.approx(seconds)

    def test_zero_remaining_blocks_until_reset(self) -> None:
        hint = parse_rate_limit_headers(
            {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "2s"}
        )
        assert hint.block_seconds == 2

    def test_anthropic_style_uses_smallest_remaining_and_latest_reset(
        self, fake_clock: Any
    ) -> None:
        now = _core.wall_time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 30))
        hint = parse_rate_limit_headers(
            {
                "anthropic-ratelimit-input-tokens-remaining": "900",
                "anthropic-ratelimit-output-tokens-remaining": "100",
                "anthropic-ratelimit-tokens-reset": stamp,
            }
        )
        assert hint.remaining_tokens == 100
        assert hint.reset_seconds == pytest.approx(30, abs=1.5)

    def test_retry_after_seconds_date_and_ms(self, fake_clock: Any) -> None:
        assert parse_rate_limit_headers({"Retry-After": "12"}).retry_after == 12
        assert parse_rate_limit_headers({"retry-after-ms": "1500"}).retry_after == 1.5
        both = parse_rate_limit_headers({"retry-after": "2", "retry-after-ms": "5000"})
        assert both.retry_after == 5
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(_core.wall_time() + 40))
        assert parse_rate_limit_headers({"retry-after": date}).retry_after == pytest.approx(
            40, abs=1.5
        )
        assert parse_rate_limit_headers({"retry-after": date}).block_seconds is not None

    def test_names_are_case_insensitive_and_pairs_and_bytes_work(self) -> None:
        assert parse_rate_limit_headers({"RETRY-AFTER": "3"}).retry_after == 3
        assert parse_rate_limit_headers([("Retry-After", "4")]).retry_after == 4
        assert parse_rate_limit_headers([(b"retry-after", b"5")]).retry_after == 5

    @pytest.mark.parametrize(
        "headers",
        [None, {}, 5, object(), {"retry-after": "soon"}, {"retry-after": "nan"},
         {"x-ratelimit-remaining-tokens": "many"}, {"retry-after": "-5"}],
    )  # fmt: skip
    def test_garbage_never_raises(self, headers: Any) -> None:
        hint = parse_rate_limit_headers(headers)
        assert hint.retry_after is None or hint.retry_after >= 0
        assert hint.remaining_tokens is None

    @given(st.dictionaries(st.text(max_size=12), st.text(max_size=20), max_size=6))
    def test_arbitrary_headers_never_raise(self, headers: dict[str, str]) -> None:
        hint = parse_rate_limit_headers(headers)
        for value in (hint.reset_seconds, hint.retry_after):
            assert value is None or value >= 0


# ----------------------------------------------------------------------- the bucket maths


class TestStep:
    def test_new_bucket_is_full_and_reserve_takes_tokens(self) -> None:
        state, wait = _step(None, "reserve", 40, 0, 100, 1, 10.0)
        assert wait == 0 and state[0] == 60

    def test_oversized_request_admitted_only_when_full(self) -> None:
        state, wait = _step(None, "reserve", 500, 0, 100, 1, 0.0)
        assert wait == 0 and state[0] == -400
        _, wait = _step(state, "reserve", 500, 0, 100, 1, 0.0)
        assert wait == pytest.approx(500)

    def test_blocked_window_returns_remaining_block(self) -> None:
        state, _ = _step(None, "observe", -1, 30, 100, 1, 0.0)
        _, wait = _step(state, "reserve", 1, 0, 100, 1, 10.0)
        assert wait == pytest.approx(20)
        _, wait = _step(state, "reserve", 1, 0, 100, 1, 30.0)
        assert wait == 0

    def test_observe_only_lowers_level(self) -> None:
        state, _ = _step(None, "observe", 30, -1, 100, 1, 0.0)
        assert state[0] == 30
        state, _ = _step(state, "observe", 80, -1, 100, 1, 0.0)
        assert state[0] == 30

    def test_unknown_capacity_keeps_unknown_level(self) -> None:
        state, _ = _step(None, "observe", -1, 5, 0, 1, 0.0)
        assert state[0] is None and state[2] == 5

    def test_unknown_op_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown window operation"):
            _step(None, "explode", 0, 0, 100, 1, 0.0)

    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["reserve", "force", "settle", "level", "observe"]),
                st.integers(0, 300),
                st.integers(0, 300),
                st.floats(0, 5, allow_nan=False),
            ),
            max_size=40,
        )
    )
    def test_level_never_exceeds_capacity_and_never_admits_past_it(
        self, ops: list[tuple[str, int, int, float]]
    ) -> None:
        state = None
        now = 0.0
        admitted = 0
        for op, a, b, dt in ops:
            now += dt
            before = state
            args = (a, b) if op != "observe" else (a if a % 2 else -1, b % 7 - 1)
            state, result = _step(state, op, float(args[0]), float(args[1]), 100, 2.0, now)
            assert state[0] is not None
            assert state[0] <= 100 + 1e-6
            if op == "reserve" and result == 0:
                admitted += 1
                level_before = (
                    100.0
                    if before is None
                    else min(100.0, (before[0] or 0) + (now - before[1]) * 2.0)
                )
                # admitted only with enough tokens (or a full bucket for oversized requests)
                assert level_before + 1e-6 >= min(a, 100)


# ---------------------------------------------------------------------------- SQLite


class TestSQLiteWindow:
    def test_two_windows_share_one_bucket(self, db: str, fake_clock: Any) -> None:
        a, b = SQLiteTokenWindow(db), SQLiteTokenWindow(db)
        assert a.reserve(400, 600, RATE) == 0
        assert b.level(600, RATE) == pytest.approx(200)
        assert b.reserve(400, 600, RATE) == pytest.approx(20)
        fake_clock.advance(20)
        assert b.reserve(400, 600, RATE) == 0

    def test_named_windows_are_independent_in_one_file(self, db: str) -> None:
        a, b = SQLiteTokenWindow(db, "a"), SQLiteTokenWindow(db, "b")
        a.reserve(500, 600, RATE)
        assert b.level(600, RATE) == pytest.approx(600)

    def test_clear_and_settle_and_force(self, db: str, fake_clock: Any) -> None:
        w = SQLiteTokenWindow(db)
        w.reserve(500, 600, RATE)
        w.settle(500, 100, 600, RATE)
        assert w.level(600, RATE) == pytest.approx(500)
        w.force(1000, 600, RATE)
        assert w.level(600, RATE) == pytest.approx(-500)
        w.clear()
        assert w.level(600, RATE) == 600

    def test_limiter_uses_store_and_reconciles_usage(self, db: str, fake_clock: Any) -> None:
        llm = fake_llm(["a", "b"], usage=(50, 50))
        limit = token_rate_limit(1000, estimate=400, block=False, store=SQLiteTokenWindow(db))

        @limit
        def ask() -> Any:
            return llm.complete(prompt="x")

        ask()
        assert limit.available == pytest.approx(900, abs=1)  # 400 reserved, settled to 100
        other = token_rate_limit(1000, estimate=950, block=False, store=SQLiteTokenWindow(db))
        assert other.try_reserve(950) > 0  # 900 left: the other worker sees our spend

    def test_blocking_limiter_waits_for_the_fleet(self, db: str, fake_clock: Any) -> None:
        a = token_rate_limit(600, estimate=600, store=SQLiteTokenWindow(db))
        b = token_rate_limit(600, estimate=600, store=SQLiteTokenWindow(db))
        a(lambda: "x")()
        b(lambda: "y")()
        assert fake_clock.sleeps and sum(fake_clock.sleeps) == pytest.approx(60)

    def test_observe_headers_makes_every_worker_back_off(self, db: str, fake_clock: Any) -> None:
        a = token_rate_limit(6000, estimate=10, block=False, store=SQLiteTokenWindow(db))
        b = token_rate_limit(6000, estimate=10, block=False, store=SQLiteTokenWindow(db))
        a.observe_headers({"retry-after": "7"})
        with pytest.raises(RateLimitExceeded) as info:
            b(lambda: "x")()
        assert info.value.retry_after == pytest.approx(7)
        fake_clock.advance(7)
        assert b(lambda: "x")() == "x"

    def test_remaining_header_lowers_shared_level(self, db: str) -> None:
        a = token_rate_limit(6000, estimate=10, store=SQLiteTokenWindow(db))
        b = token_rate_limit(6000, estimate=10, store=SQLiteTokenWindow(db))
        a.observe_headers({"x-ratelimit-remaining-tokens": "250"})
        assert b.available == pytest.approx(250, abs=1)
        a.observe_headers({"x-ratelimit-remaining-tokens": "9000"})  # never raises it
        assert b.available <= 251

    def test_429_error_headers_are_fed_back_automatically(self, db: str, fake_clock: Any) -> None:
        a = token_rate_limit(6000, estimate=10, block=False, store=SQLiteTokenWindow(db))
        b = token_rate_limit(6000, estimate=10, block=False, store=SQLiteTokenWindow(db))
        llm = fake_llm([])

        @a
        def call() -> str:
            raise RateLimitError("slow down", status_code=429, retry_after=9)

        with pytest.raises(RateLimitError):
            call()
        assert llm.call_count == 0
        with pytest.raises(RateLimitExceeded) as info:
            b(lambda: "x")()
        assert info.value.retry_after == pytest.approx(9)

    def test_local_limiter_honours_headers_too(self, fake_clock: Any) -> None:
        limit = token_rate_limit(6000, estimate=10, block=False)
        limit.observe_headers({"retry-after": "4"})
        with pytest.raises(RateLimitExceeded) as info:
            limit(lambda: 1)()
        assert info.value.retry_after == pytest.approx(4)
        fake_clock.advance(4)
        assert limit(lambda: 1)() == 1
        limit.observe_headers({"x-ratelimit-remaining-tokens": "3"})
        assert limit.available == pytest.approx(3, abs=1)

    def test_async_limiter_with_sync_store_runs_in_thread(self, db: str, fake_clock: Any) -> None:
        limit = token_rate_limit(600, estimate=600, store=SQLiteTokenWindow(db))

        @limit
        async def call() -> str:
            return "ok"

        async def main() -> None:
            await call()
            await call()
            await limit.aobserve_headers({"retry-after": "1"})

        asyncio.run(main())
        assert sum(fake_clock.sleeps) >= 60

    def test_threads_never_over_admit(self, db: str, fake_clock: Any) -> None:
        window = SQLiteTokenWindow(db)

        def take(_: int) -> bool:
            return window.reserve(1, 50, 0.0001) == 0

        with ThreadPoolExecutor(8) as pool:
            admitted = sum(pool.map(take, range(200)))
        assert admitted == 50

    def test_pickling_refused_like_other_limiters(self, db: str) -> None:
        import pickle

        with pytest.raises(Exception):  # noqa: B017 - the exact class is python_wrap's
            pickle.dumps(token_rate_limit(100, store=SQLiteTokenWindow(db)))

    def test_layer_marks_the_limiter_shared(self, db: str) -> None:
        limit = token_rate_limit(100, store=SQLiteTokenWindow(db))
        assert limit.__python_wrap_layer__[1]["shared"] is True
        assert token_rate_limit(100).__python_wrap_layer__[1]["shared"] is False


class TestSQLiteFailures:
    def test_unusable_path_falls_open_to_a_local_bucket(self, tmp_path: Path) -> None:
        events: list[str] = []
        _core.listeners.append(lambda e: events.append(e.name))
        try:
            window = SQLiteTokenWindow(tmp_path / "missing" / "dir" / "t.db")
            assert window.reserve(60, 100, 1.0) == 0
            assert window.reserve(60, 100, 1.0) > 0  # the fallback still limits, per process
        finally:
            _core.listeners.pop()
        assert "rate_limit.store_error" in events

    def test_fail_closed_raises(self, tmp_path: Path) -> None:
        window = SQLiteTokenWindow(tmp_path / "missing" / "t.db", fail_open=False)
        with pytest.raises(sqlite3.OperationalError):
            window.reserve(1, 100, 1.0)

    def test_corrupt_database_is_quarantined_and_rebuilt(self, db: str) -> None:
        Path(db).write_bytes(b"this is definitely not a sqlite database" * 50)
        window = SQLiteTokenWindow(db, fail_open=False)
        assert window.reserve(10, 100, 1.0) == 0
        assert window.level(100, 1.0) == pytest.approx(90, abs=0.5)
        assert Path(db + ".corrupt").exists()

    def test_database_corrupted_mid_life_recovers(self, db: str) -> None:
        window = SQLiteTokenWindow(db, fail_open=False)
        window.reserve(10, 100, 1.0)
        size = os.path.getsize(db)
        with open(db, "r+b") as handle:  # overwrite the header and a page: a partial write
            handle.write(b"\x00" * min(size, 4096))
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.unlink(db + suffix)
        assert window.reserve(10, 100, 1.0) == 0  # rebuilt, never an exception

    def test_truncated_database_file(self, db: str) -> None:
        window = SQLiteTokenWindow(db, fail_open=False)
        window.reserve(10, 100, 1.0)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.unlink(db + suffix)
        with open(db, "r+b") as handle:
            handle.truncate(100)
        assert window.reserve(10, 100, 1.0) == 0

    def test_locked_database_times_out_then_falls_open(self, db: str) -> None:
        window = SQLiteTokenWindow(db, timeout=0.05)
        window.reserve(1, 100, 1.0)
        blocker = sqlite3.connect(db, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            assert window.reserve(1, 100, 1.0) == 0  # fell open, did not hang or raise
            strict = SQLiteTokenWindow(db, timeout=0.05, fail_open=False)
            with pytest.raises(sqlite3.OperationalError):
                strict.reserve(1, 100, 1.0)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_name_required(self, db: str) -> None:
        with pytest.raises(ValueError):
            SQLiteTokenWindow(db, "")

    def test_base_class_is_abstract(self) -> None:
        with pytest.raises(NotImplementedError):
            SharedTokenWindow("x", fail_open=False).reserve(1, 1, 1)


# ---------------------------------------------------------------- budget backend (SQLite)


class TestSQLiteBudget:
    def test_budget_shared_between_instances(self, db: str) -> None:
        one = Budget(tokens=1000, backend=SQLiteBudgetBackend(db))
        two = Budget(tokens=1000, backend=SQLiteBudgetBackend(db))
        one.record({"input_tokens": 600, "output_tokens": 0})
        two.record({"input_tokens": 300, "output_tokens": 0})
        assert one.spent().tokens == 900
        assert not one.exhausted()
        two.record({"input_tokens": 200, "output_tokens": 0})
        assert one.exhausted()
        one.reset()
        assert two.spent().tokens == 0

    def test_ttl_expiry_uses_the_wall_clock(self, db: str, fake_clock: Any) -> None:
        backend = SQLiteBudgetBackend(db)
        assert backend.add("k", 5, 0.5, 10) == (5, 0.5)
        assert backend.add("k", 5, 0.5, 10) == (10, 1.0)
        fake_clock.advance(11)
        assert backend.get("k") == (0, 0.0)
        assert backend.add("k", 1, 0.0, 10) == (1, 0.0)

    def test_concurrent_adds_lose_nothing(self, db: str) -> None:
        backend = SQLiteBudgetBackend(db)
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: backend.add("k", 1, 0.25, None), range(100)))
        assert backend.get("k") == (100, 25.0)

    def test_corrupt_file_recovers_and_unknown_key_is_zero(self, db: str) -> None:
        Path(db).write_bytes(b"garbage" * 200)
        backend = SQLiteBudgetBackend(db)
        assert backend.get("nothing") == (0, 0.0)
        assert Path(db + ".corrupt").exists()

    def test_budget_decorator_with_sqlite_backend(self, db: str) -> None:
        budget = Budget(tokens=100, backend=SQLiteBudgetBackend(db))
        llm = fake_llm(["a", "b", "c"], usage=(30, 30))

        @budget
        def ask() -> Any:
            return llm.complete(prompt="p")

        ask()  # 60 of 100 tokens
        ask()  # the check happens before the call, so this one still runs
        with pytest.raises(python_wrap.BudgetExceeded):
            ask()


# -------------------------------------------------------------- cross-process admission

_WORKER = textwrap.dedent(
    """
    import sys, time
    from python_wrap.llm_shared import SQLiteTokenWindow

    path, tries, tokens, capacity, rate, start = sys.argv[1:7]
    window = SQLiteTokenWindow(path, fail_open=False, timeout=60)
    while time.time() < float(start):
        time.sleep(0.001)
    admitted = 0
    for _ in range(int(tries)):
        if window.reserve(int(tokens), float(capacity), float(rate)) == 0:
            admitted += int(tokens)
    print(admitted)
    """
)


def _run_workers(
    db: str, procs: int, tries: int, tokens: int, capacity: int, rate: float
) -> tuple[int, float]:
    src = str(Path(python_wrap.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src}
    start = time.time() + 1.0
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                db,
                str(tries),
                str(tokens),
                str(capacity),
                str(rate),
                str(start),
            ],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(procs)
    ]
    total = 0
    for child in children:
        out, _ = child.communicate(timeout=120)
        assert child.returncode == 0
        total += int(out.strip())
    return total, time.time() - start


@pytest.mark.parametrize("procs", [2, 4])
def test_processes_never_admit_more_than_the_bucket_allows(db: str, procs: int) -> None:
    capacity, rate = 100, 2.0
    total, elapsed = _run_workers(db, procs, tries=120, tokens=1, capacity=capacity, rate=rate)
    assert total >= capacity  # the bucket really was used
    assert total <= capacity + rate * (elapsed + 0.5) + 1


@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    procs=st.integers(2, 4),
    capacity=st.integers(10, 60),
    tokens=st.integers(1, 5),
)
def test_property_cross_process_admission_is_bounded(
    tmp_path_factory: pytest.TempPathFactory, procs: int, capacity: int, tokens: int
) -> None:
    db = str(tmp_path_factory.mktemp("win") / "t.db")
    rate = 1.0
    total, elapsed = _run_workers(db, procs, tries=40, tokens=tokens, capacity=capacity, rate=rate)
    assert total <= capacity + rate * (elapsed + 0.5) + tokens


# --------------------------------------------------------------------------------- Redis


class FakeRedis:
    """Evaluates the window script with the Python mirror of its logic."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.fail = False

    def eval(
        self, script: str, count: int, key: str, op: str, a: str, b: str, cap: str, rate: str
    ) -> bytes:
        if self.fail:
            raise ConnectionError("redis is down")
        with self.lock:
            new, result = _step(
                self.state.get(key),
                op,
                float(a),
                float(b),
                float(cap),
                float(rate),
                _core.wall_time(),
            )
            self.state[key] = new
        return f"{result:.9f}".encode()


class AsyncFakeRedis(FakeRedis):
    async def eval(self, *args: Any) -> bytes:  # type: ignore[override]
        return super().eval(*args)


class TestRedisWindowWithFake:
    def test_shares_state_by_name_and_prefix(self, fake_clock: Any) -> None:
        client = FakeRedis()
        a, b = RedisTokenWindow(client, "q"), RedisTokenWindow(client, "q")
        other = RedisTokenWindow(client, "other")
        assert a.reserve(400, 600, RATE) == 0
        assert b.level(600, RATE) == pytest.approx(200)
        assert other.level(600, RATE) == 600
        assert set(client.state) == {"python-wrap:tokens:q", "python-wrap:tokens:other"}

    def test_limiter_and_headers(self, fake_clock: Any) -> None:
        client = FakeRedis()
        limit = token_rate_limit(600, estimate=10, block=False, store=RedisTokenWindow(client))
        peer = token_rate_limit(600, estimate=10, block=False, store=RedisTokenWindow(client))
        limit.observe_headers(
            {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "8s"}
        )
        with pytest.raises(RateLimitExceeded) as info:
            peer(lambda: 1)()
        assert info.value.retry_after == pytest.approx(8)

    def test_outage_falls_open_or_raises(self) -> None:
        client = FakeRedis()
        client.fail = True
        assert RedisTokenWindow(client).reserve(1, 100, 1.0) == 0
        with pytest.raises(ConnectionError):
            RedisTokenWindow(client, fail_open=False).reserve(1, 100, 1.0)

    def test_bad_prefix(self) -> None:
        with pytest.raises(ValueError):
            RedisTokenWindow(FakeRedis(), prefix="")
        with pytest.raises(ValueError):
            AsyncRedisTokenWindow(AsyncFakeRedis(), prefix="")

    def test_async_window_in_async_limiter(self, fake_clock: Any) -> None:
        client = AsyncFakeRedis()
        limit = token_rate_limit(600, estimate=600, store=AsyncRedisTokenWindow(client))

        @limit
        async def call() -> str:
            return "ok"

        async def main() -> None:
            await call()
            await call()
            await limit.aobserve_headers({"retry-after": "3"})
            hint = await AsyncRedisTokenWindow(client).aobserve_headers({"retry-after": "1"})
            assert hint.retry_after == 1
            assert await AsyncRedisTokenWindow(client).alevel(600, RATE) <= 600

        asyncio.run(main())
        assert sum(fake_clock.sleeps) >= 60

    def test_async_window_refuses_sync_use_and_falls_open_on_outage(self) -> None:
        client = AsyncFakeRedis()
        window = AsyncRedisTokenWindow(client, fail_open=False)
        with pytest.raises(RuntimeError, match="async-only"):
            window.reserve(1, 1, 1)
        client.fail = True

        async def main() -> None:
            with pytest.raises(ConnectionError):
                await window.areserve(1, 100, 1.0)
            assert await AsyncRedisTokenWindow(client).areserve(1, 100, 1.0) == 0

        asyncio.run(main())


@pytest.mark.skipif(redis_client is None, reason="redis fixture unavailable")
class TestRedisWindowLive:
    """The Lua script against a real redis-server, including parity with the Python mirror."""

    def test_script_matches_mirror_and_is_shared(self, redis_client: Any) -> None:
        a = RedisTokenWindow(redis_client, "live-a", prefix="pw-test", fail_open=False)
        b = RedisTokenWindow(redis_client, "live-a", prefix="pw-test", fail_open=False)
        assert a.reserve(400, 600, RATE) == 0
        assert b.level(600, RATE) == pytest.approx(200, abs=2)
        wait = b.reserve(400, 600, RATE)
        assert wait == pytest.approx(20, abs=1)
        b.settle(400, 0, 600, RATE)  # refund
        assert a.level(600, RATE) == pytest.approx(600, abs=2)
        a.force(1000, 600, RATE)
        assert a.level(600, RATE) == pytest.approx(-400, abs=2)

    def test_observe_blocks_and_expires_with_ttl(self, redis_client: Any) -> None:
        w = RedisTokenWindow(redis_client, "live-b", prefix="pw-test", fail_open=False)
        w.observe_headers({"retry-after": "30"}, capacity=600, rate=RATE)
        assert 28 < w.reserve(1, 600, RATE) <= 30
        ttl = redis_client.execute("PTTL", "pw-test:live-b")
        assert ttl > 30000

    def test_concurrent_reservations_never_over_admit(self, redis_client: Any) -> None:
        w = RedisTokenWindow(redis_client, "live-c", prefix="pw-test", fail_open=False)

        def take(_: int) -> bool:
            return w.reserve(1, 40, 0.0001) == 0

        with ThreadPoolExecutor(8) as pool:
            assert sum(pool.map(take, range(120))) == 40
