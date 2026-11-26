"""Share circuit breaker state between processes on one host, without Redis.

`SQLiteBreakerState` is a drop-in ``backend=`` for `CircuitBreaker` with the same
contract and semantics as `RedisBreakerState`, stored in a local SQLite file. Use it for
gunicorn or uvicorn workers and Celery prefork children on a single machine: one
worker's failures open the breaker for all of them.

Every operation is one ``BEGIN IMMEDIATE`` transaction (atomic across processes), the
database runs in WAL mode, and each thread of each process gets its own connection,
reopened after a fork. Time is the wall clock (`_core.wall_time`), the only clock the
processes share.
"""

from __future__ import annotations

import contextlib
import math
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

from python_wrap import _core, _fork

__all__ = ["SQLiteBreakerState"]

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS breakers (
        name TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'closed',
        gen INTEGER NOT NULL DEFAULT 0,
        opened INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        trials INTEGER NOT NULL DEFAULT 0,
        successes INTEGER NOT NULL DEFAULT 0,
        trial_deadline INTEGER NOT NULL DEFAULT 0,
        total_failures INTEGER NOT NULL DEFAULT 0,
        total_successes INTEGER NOT NULL DEFAULT 0,
        total_rejected INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS breaker_failures (
        name TEXT NOT NULL,
        ts INTEGER NOT NULL,
        id TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS breaker_failures_name_ts ON breaker_failures (name, ts)",
    """CREATE TABLE IF NOT EXISTS breaker_slots (
        name TEXT NOT NULL,
        slot INTEGER NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        slow INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (name, slot)
    )""",
)

_COLUMNS = (
    "state",
    "gen",
    "opened",
    "failures",
    "trials",
    "successes",
    "trial_deadline",
    "total_failures",
    "total_successes",
    "total_rejected",
)


class SQLiteBreakerState:
    """Breaker state in a local SQLite file, shared by every process that opens it.

    Use the same path, and the same breaker name and settings, in every process. The
    directory must exist and be writable by all of them. Backend errors (for example a
    database that stays locked longer than ``busy_timeout``) propagate; they are never
    read as a healthy dependency. Half-open reservations expire after
    ``max(1s, recovery_timeout)``, so a crashed probing worker cannot hold the trial
    slot forever. Not suitable for network file systems.

    Args:
        path: Database file path (created on first use). ``":memory:"`` is rejected:
            each connection would get a private database.
        busy_timeout: Seconds to wait for another process's transaction.

    Raises:
        ValueError: For an empty or in-memory path, or a negative ``busy_timeout``.

    Example:
        ```python
        import os
        import tempfile

        from python_wrap import CircuitBreaker
        from python_wrap.local_state import SQLiteBreakerState

        with tempfile.TemporaryDirectory() as tmp:
            state = SQLiteBreakerState(os.path.join(tmp, "breakers.db"))
            breaker = CircuitBreaker(name="vendor-api", backend=state, failure_threshold=1)
            breaker.force_open()
            other = CircuitBreaker(name="vendor-api", backend=state, failure_threshold=1)
            assert other.state.value == "open"
            state.close()
        ```
    """

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout: float = 5.0) -> None:
        text = os.fspath(path)
        if not text or text == ":memory:" or text.startswith("file::memory:"):
            raise ValueError("path must name a file shared between processes")
        if not math.isfinite(busy_timeout) or busy_timeout < 0:
            raise ValueError("busy_timeout must be finite and >= 0")
        self.path, self.busy_timeout = text, float(busy_timeout)
        self._local = threading.local()
        self._lock = _fork.ForkSafeLock()
        self._conns: list[sqlite3.Connection] = []
        self._abandoned: list[sqlite3.Connection] = []
        self._pid = os.getpid()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        # Never use or close a connection inherited across fork; just park it.
        self._abandoned.extend(self._conns)
        self._conns = []
        self._local = threading.local()
        self._pid = os.getpid()

    # -- connections -------------------------------------------------------------------
    def _setup(self, conn: sqlite3.Connection) -> None:
        """Switch to WAL and create the schema.

        Changing the journal mode takes a lock that SQLite does not always wait for with
        the busy handler, so concurrent first connections (threads or processes) can see
        "database is locked" immediately. Retry until ``busy_timeout`` has elapsed.
        """
        deadline = time.monotonic() + self.busy_timeout
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                for statement in _SCHEMA:
                    conn.execute(statement)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def _connection(self) -> sqlite3.Connection:
        if self._pid != os.getpid():  # fork without the at-fork hook (non-POSIX paths)
            self._reset_after_fork()
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
            self._setup(conn)
        except BaseException:
            conn.close()
            raise
        self._local.conn = conn
        with self._lock:
            self._conns.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this process opened. The state stays in the file."""
        with self._lock:
            conns, self._conns = self._conns, []
        self._local = threading.local()
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    # -- the contract ------------------------------------------------------------------
    def operate(
        self,
        breaker: Any,
        operation: str,
        generation: int = 0,
        trial: bool = False,
        slow: bool = False,
    ) -> dict[str, Any]:
        """Apply one breaker operation atomically and return the resulting row.

        Args:
            breaker: A `CircuitBreaker` (its name and settings are read).
            operation: ``state``, ``admit``, ``success``, ``failure``, ``neutral``,
                ``reset`` or ``force_open``.
            generation: The generation the call was admitted under; outcomes from an
                older generation are ignored.
            trial: Whether the call was a half-open trial.
            slow: Whether the call took longer than the breaker's ``slow_call_duration``.

        Returns:
            A dict with the same keys as `RedisBreakerState.operate`.
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            data = self._apply(conn, breaker, operation, generation, trial, slow)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return data

    @staticmethod
    def _slots(
        conn: sqlite3.Connection,
        name: str,
        now: int,
        window: int,
        calls: int,
        failed: int,
        slow: int,
    ) -> tuple[int, int, int]:
        """Record an outcome in its time slot (ten per window) and sum the live slots."""
        slot = now // max(1, window // 10)
        conn.execute("DELETE FROM breaker_slots WHERE name=? AND slot<=?", (name, slot - 10))
        if calls:
            conn.execute(
                "INSERT INTO breaker_slots (name, slot, calls, failures, slow) VALUES (?,?,?,?,?)"
                " ON CONFLICT (name, slot) DO UPDATE SET calls=calls+excluded.calls,"
                " failures=failures+excluded.failures, slow=slow+excluded.slow",
                (name, slot, calls, failed, slow),
            )
        row = conn.execute(
            "SELECT COALESCE(SUM(calls),0), COALESCE(SUM(failures),0), COALESCE(SUM(slow),0)"
            " FROM breaker_slots WHERE name=?",
            (name,),
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def _apply(
        self,
        conn: sqlite3.Connection,
        breaker: Any,
        op: str,
        generation: int,
        is_trial: bool,
        is_slow: bool = False,
    ) -> dict[str, Any]:
        name: str = breaker.name
        frate = getattr(breaker, "failure_rate", None) or 0
        srate = getattr(breaker, "slow_call_rate", None) or 0
        minc, rated, slow = getattr(breaker, "minimum_calls", 1), bool(frate or srate), int(is_slow)
        wc = wf = ws = 0
        trip = False

        def trips() -> bool:
            return wc >= minc and bool((frate and wf >= frate * wc) or (srate and ws >= srate * wc))

        now = int(_core.wall_time() * 1000)
        threshold = breaker.failure_threshold
        window = 0 if breaker.window is None else max(1, math.ceil(breaker.window * 1000))
        reset = math.ceil(breaker.recovery_timeout * 1000)
        halfmax, target = breaker.half_open_max_calls, breaker.success_threshold

        conn.execute("INSERT OR IGNORE INTO breakers (name) VALUES (?)", (name,))
        # _COLUMNS is a module constant; the only value is bound as a parameter.
        query = f"SELECT {', '.join(_COLUMNS)} FROM breakers WHERE name=?"  # nosec B608
        row = conn.execute(query, (name,))
        (
            state,
            gen,
            opened,
            failures,
            trials,
            successes,
            deadline,
            total_failures,
            total_successes,
            total_rejected,
        ) = row.fetchone()
        old = state
        admitted, trial, wait = 1, 0, 0

        if op == "reset":
            state, failures, trials, successes, opened = "closed", 0, 0, 0, 0
            gen += 1
            total_failures = total_successes = total_rejected = 0
            conn.execute("DELETE FROM breaker_failures WHERE name=?", (name,))
            conn.execute("DELETE FROM breaker_slots WHERE name=?", (name,))
        elif op == "force_open":
            state, opened, trials = "open", now, 0
            gen += 1
        else:
            if state == "open" and now - opened >= reset:
                state, trials, successes = "half_open", 0, 0
                gen += 1
                deadline = now + max(1000, reset)
            if op == "admit":
                if state == "half_open" and now >= deadline:
                    gen += 1
                    trials = successes = 0
                    deadline = now + max(1000, reset)
                if state == "open" or (state == "half_open" and trials >= halfmax):
                    admitted = 0
                    wait = max(0, opened + reset - now)
                    total_rejected += 1
                elif state == "half_open":
                    trial = 1
                    trials += 1
            elif op in ("success", "failure", "neutral") and generation == gen:
                if is_trial:
                    trials = max(0, trials - 1)
                if op == "success":
                    total_successes += 1
                    if state == "half_open" and slow and srate:
                        trip = True
                    elif state == "half_open":
                        successes += 1
                        if successes >= target:
                            state, failures = "closed", 0
                            gen += 1
                            conn.execute("DELETE FROM breaker_failures WHERE name=?", (name,))
                            conn.execute("DELETE FROM breaker_slots WHERE name=?", (name,))
                    else:
                        if window == 0:
                            failures = 0
                        if rated and not is_trial:
                            wc, wf, ws = self._slots(conn, name, now, window, 1, 0, slow)
                            trip = trips()
                elif op == "failure":
                    total_failures += 1
                    if rated and not is_trial and state != "half_open":
                        wc, wf, ws = self._slots(conn, name, now, window, 1, 1, slow)
                        trip = trips()
                    if frate:
                        failures = wf
                    elif window > 0:
                        conn.execute(
                            "DELETE FROM breaker_failures WHERE name=? AND ts<=?",
                            (name, now - window),
                        )
                        conn.execute(
                            "INSERT INTO breaker_failures (name, ts, id) VALUES (?, ?, ?)",
                            (name, now, uuid.uuid4().hex),
                        )
                        failures = conn.execute(
                            "SELECT COUNT(*) FROM breaker_failures WHERE name=?", (name,)
                        ).fetchone()[0]
                    else:
                        failures += 1
                    if state == "half_open" or (not frate and failures >= threshold):
                        trip = True
                if trip:
                    state, opened, trials, successes = "open", now, 0, 0
                    gen += 1
        if rated and wc == 0:
            wc, wf, ws = self._slots(conn, name, now, window, 0, 0, 0)

        conn.execute(
            "UPDATE breakers SET state=?, gen=?, opened=?, failures=?, trials=?, successes=?,"
            " trial_deadline=?, total_failures=?, total_successes=?, total_rejected=?"
            " WHERE name=?",
            (
                state,
                gen,
                opened,
                failures,
                trials,
                successes,
                deadline,
                total_failures,
                total_successes,
                total_rejected,
                name,
            ),
        )
        return {
            "admitted": admitted,
            "state": state,
            "generation": gen,
            "trial": trial,
            "wait": wait,
            "failures": failures,
            "total_failures": total_failures,
            "total_successes": total_successes,
            "total_rejected": total_rejected,
            "opened": opened,
            "old": old,
            "window_calls": wc,
            "window_failures": wf,
            "window_slow": ws,
        }
