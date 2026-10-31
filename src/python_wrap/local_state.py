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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            for statement in _SCHEMA:
                conn.execute(statement)
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
        self, breaker: Any, operation: str, generation: int = 0, trial: bool = False
    ) -> dict[str, Any]:
        """Apply one breaker operation atomically and return the resulting row.

        Args:
            breaker: A `CircuitBreaker` (its name and settings are read).
            operation: ``state``, ``admit``, ``success``, ``failure``, ``neutral``,
                ``reset`` or ``force_open``.
            generation: The generation the call was admitted under; outcomes from an
                older generation are ignored.
            trial: Whether the call was a half-open trial.

        Returns:
            A dict with the same keys as `RedisBreakerState.operate`.
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            data = self._apply(conn, breaker, operation, generation, trial)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return data

    def _apply(
        self, conn: sqlite3.Connection, breaker: Any, op: str, generation: int, is_trial: bool
    ) -> dict[str, Any]:
        name: str = breaker.name
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
                    if state == "half_open":
                        successes += 1
                        if successes >= target:
                            state, failures = "closed", 0
                            gen += 1
                            conn.execute("DELETE FROM breaker_failures WHERE name=?", (name,))
                    elif window == 0:
                        failures = 0
                elif op == "failure":
                    total_failures += 1
                    if window > 0:
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
                    if state == "half_open" or failures >= threshold:
                        state, opened, trials, successes = "open", now, 0, 0
                        gen += 1

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
        }
