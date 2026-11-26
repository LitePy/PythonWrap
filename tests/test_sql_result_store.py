"""SqlResultStore and idempotent(connection=...) on SQLite (needs SQLAlchemy)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from python_wrap import AlreadyRunning
from python_wrap.idempotency import SqlResultStore, idempotent
from python_wrap.testing import fake_clock

sqlalchemy = pytest.importorskip("sqlalchemy")


def sqlite_engine(path: Path) -> Any:
    """pysqlite skips BEGIN, which breaks savepoints; this is SQLAlchemy's documented fix."""
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")

    @sqlalchemy.event.listens_for(engine, "connect")
    def _connect(dbapi_connection: Any, record: Any) -> None:
        dbapi_connection.isolation_level = None

    @sqlalchemy.event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    engine = sqlite_engine(tmp_path / "app.db")
    with engine.begin() as conn:
        for statement in SqlResultStore.ddl("sqlite"):
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT)")
    yield engine
    engine.dispose()


def test_plain_store_round_trip_expiry_and_purge(engine: Any) -> None:
    store = SqlResultStore(engine)
    with fake_clock() as clock:
        store.set("a", ({"total": 3},), 10)
        store.set("a", ({"total": 4},), 10)  # overwrite
        assert store.get("a") == ({"total": 4},)
        assert store.get("missing") is None
        clock.advance(11)
        assert store.get("a") is None
        assert store.purge_expired() == 1
    assert store.delete("a") is False


def test_idempotent_with_plain_sql_store(engine: Any) -> None:
    calls: list[int] = []

    @idempotent(key=lambda n: f"job-{n}", store=SqlResultStore(engine))
    def job(n: int) -> int:
        calls.append(n)
        return n * 10

    assert job(1) == job(1) == 10
    assert calls == [1]


def test_key_row_commits_with_the_business_write(engine: Any) -> None:
    store = SqlResultStore(engine)
    current: dict[str, Any] = {}

    @idempotent(key=lambda sku: f"order:{sku}", store=store, connection=lambda: current["conn"])
    def place(sku: str) -> str:
        current["conn"].exec_driver_sql("INSERT INTO orders (sku) VALUES (?)", (sku,))
        return f"placed {sku}"

    with engine.connect() as conn:  # a request whose transaction fails after the call
        current["conn"] = conn
        with pytest.raises(RuntimeError), conn.begin():
            assert place("x1") == "placed x1"
            raise RuntimeError("request failed before commit")
    for _ in range(2):
        with engine.begin() as conn:
            current["conn"] = conn
            assert place("x1") == "placed x1"
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 1


def test_live_claim_follows_in_progress(engine: Any) -> None:
    store = SqlResultStore(engine)
    with engine.begin() as conn:  # someone committed a running claim early
        assert store._claim(conn, "busy", "other", 60)
    with engine.begin() as conn:
        for mode, expected in (("return_none", None),):
            guarded = idempotent(
                key="busy", store=store, connection=lambda: conn, in_progress=mode
            )(lambda: "ran")
            assert guarded() is expected
        with pytest.raises(AlreadyRunning):
            idempotent(key="busy", store=store, connection=lambda: conn)(lambda: "ran")()


def test_process_killed_before_commit_leaves_one_row_after_retry(
    engine: Any, tmp_path: Path
) -> None:
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from test_sql_result_store import sqlite_engine
        from python_wrap.idempotency import SqlResultStore, idempotent
        engine = sqlite_engine({str(tmp_path / "app.db")!r})
        store = SqlResultStore(engine)
        with engine.begin() as conn:
            @idempotent(key="order:k1", store=store, connection=lambda: conn)
            def place():
                conn.exec_driver_sql("INSERT INTO orders (sku) VALUES ('k1')")
                os._exit(1)  # killed between the write and the return
            place()
        """
    )
    assert subprocess.run([sys.executable, "-c", script], check=False).returncode == 1
    store = SqlResultStore(engine)
    with engine.begin() as conn:

        @idempotent(key="order:k1", store=store, connection=lambda: conn)
        def place() -> str:
            conn.exec_driver_sql("INSERT INTO orders (sku) VALUES ('k1')")
            return "ok"

        assert place() == "ok"
    with engine.begin() as conn:
        assert place() == "ok"
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM orders").scalar() == 1


def test_validation() -> None:
    with pytest.raises(ValueError):
        SqlResultStore(object(), table="x; DROP TABLE y")
    with pytest.raises(ValueError):
        SqlResultStore.ddl("oracle")
    with pytest.raises(TypeError):
        idempotent(connection=lambda: None)
    store = SqlResultStore(object())
    with pytest.raises(TypeError):

        @idempotent(store=store, connection=lambda: None)
        async def nope() -> None: ...

    assert "NVARCHAR(MAX)" in SqlResultStore.ddl("mssql")[0]
