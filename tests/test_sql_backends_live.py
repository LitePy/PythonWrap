"""Live PostgreSQL and SQL Server checks for the SQL lease backends and DB predicates.

Skipped unless a SQLAlchemy URL is provided (and SQLAlchemy plus a driver are installed):

    PW_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/db
    PW_MSSQL_URL=mssql+pymssql://sa:pass@localhost:1433/master

CI runs them against service containers in the ``sql-backends`` job. Set
``PW_REQUIRE_SQL=1`` to turn a missing URL or driver into a failure instead of a skip.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from python_wrap import (
    AlreadyRunning,
    Lease,
    PgAdvisoryLock,
    SqlAppLock,
    db_transient,
    postgres_transient,
    retry,
    single_instance,
    sqlserver_transient,
)
from python_wrap.idempotency import SqlResultStore, idempotent

REQUIRED = os.environ.get("PW_REQUIRE_SQL") == "1"


def _engine(variable: str) -> Iterator[Any]:
    url = os.environ.get(variable)
    if not url:
        if REQUIRED:
            pytest.fail(f"{variable} is required (PW_REQUIRE_SQL=1)")
        pytest.skip(f"{variable} not set")
    try:
        sqlalchemy = pytest.importorskip("sqlalchemy")
        engine = sqlalchemy.create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except pytest.skip.Exception:
        if REQUIRED:
            pytest.fail("SQLAlchemy and a driver are required (PW_REQUIRE_SQL=1)")
        raise
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Any]:
    yield from _engine("PW_POSTGRES_URL")


@pytest.fixture(scope="module")
def mssql_engine() -> Iterator[Any]:
    yield from _engine("PW_MSSQL_URL")


@pytest.fixture(params=["postgres", "mssql"])
def lock_backend(request: Any) -> tuple[Any, Any]:
    if request.param == "postgres":
        engine = request.getfixturevalue("pg_engine")
        return PgAdvisoryLock(engine), PgAdvisoryLock(engine)
    engine = request.getfixturevalue("mssql_engine")
    return SqlAppLock(engine), SqlAppLock(engine)


def test_session_locks_exclude_other_sessions(lock_backend):
    first, second = lock_backend
    key = f"live-{uuid.uuid4().hex}"
    assert first.acquire(key, "owner-a", 30)
    assert not second.acquire(key, "owner-b", 30)
    assert first.renew(key, "owner-a", 30)
    assert not first.release(key, "owner-b")  # wrong token never releases
    assert first.release(key, "owner-a")
    assert second.acquire(key, "owner-b", 30)
    assert second.release(key, "owner-b")


def test_lease_and_single_instance_on_real_backend(lock_backend):
    first, second = lock_backend
    key = f"live-{uuid.uuid4().hex}"
    with (
        Lease(key, backend=first, ttl=30, renew=False),
        pytest.raises(AlreadyRunning),
        Lease(key, backend=second, ttl=30, renew=False),
    ):
        pass
    runs = []

    @single_instance(key=key, backend=second, ttl=30)
    def job() -> str:
        runs.append(1)
        return "done"

    assert job() == "done" and runs == [1]


def test_postgres_errors_are_classified(pg_engine):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    table = f"pw_live_{uuid.uuid4().hex[:8]}"
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE {table} (id int PRIMARY KEY)")
    try:
        with pytest.raises(sqlalchemy.exc.IntegrityError) as dup, pg_engine.begin() as conn:
            conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1), (1)")
        assert not postgres_transient(dup.value) and not db_transient(dup.value)
    finally:
        with pg_engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE {table}")

    # A server-side disconnect (admin shutdown, 57P01) is transient and retried.
    attempts = []

    @retry(max_attempts=2, delay=0, retry_if=db_transient)
    def disconnected() -> int:
        attempts.append(1)
        with pg_engine.connect() as conn:
            if len(attempts) == 1:
                conn.exec_driver_sql("SELECT pg_terminate_backend(pg_backend_pid())")
            return int(conn.exec_driver_sql("SELECT 1").scalar())

    assert disconnected() == 1
    assert len(attempts) == 2


def test_sqlserver_errors_are_classified(mssql_engine):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    with pytest.raises(sqlalchemy.exc.DBAPIError) as zero, mssql_engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1/0").scalar()
    assert not sqlserver_transient(zero.value) and not db_transient(zero.value)
    # A lock request timeout (1222) is one of the documented transient numbers.
    table = f"pw_live_{uuid.uuid4().hex[:8]}"
    with mssql_engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE {table} (id int)")
    try:
        with mssql_engine.connect() as holder, mssql_engine.connect() as waiter:
            holder.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")  # open transaction
            waiter.exec_driver_sql("SET LOCK_TIMEOUT 100")
            with pytest.raises(sqlalchemy.exc.DBAPIError) as timeout_error:
                waiter.exec_driver_sql(f"SELECT * FROM {table}").fetchall()
            holder.rollback()
        assert sqlserver_transient(timeout_error.value)
        assert db_transient(timeout_error.value)
    finally:
        with mssql_engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE {table}")


@pytest.fixture(params=["postgres", "mssql"])
def result_table(request: Any) -> Iterator[tuple[Any, str]]:
    engine = request.getfixturevalue("pg_engine" if request.param == "postgres" else "mssql_engine")
    table = f"pw_idem_{uuid.uuid4().hex[:8]}"
    dialect = "postgresql" if request.param == "postgres" else "mssql"
    with engine.begin() as conn:
        for statement in SqlResultStore.ddl(dialect, table):
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql(f"CREATE TABLE {table}_orders (id int)")
    yield engine, table
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE {table}")
        conn.exec_driver_sql(f"DROP TABLE {table}_orders")


def test_sql_result_store_key_commits_with_the_write(result_table):
    engine, table = result_table
    store = SqlResultStore(engine, table)
    store.set("plain", ({"n": 1},), 60)
    assert store.get("plain") == ({"n": 1},)
    current = {}

    @idempotent(key="order-1", store=store, connection=lambda: current["conn"])
    def place() -> str:
        current["conn"].exec_driver_sql(f"INSERT INTO {table}_orders VALUES (1)")
        return "placed"

    with engine.connect() as conn:
        current["conn"] = conn
        with pytest.raises(RuntimeError), conn.begin():
            place()
            raise RuntimeError("rolled back")
    for _ in range(2):
        with engine.begin() as conn:
            current["conn"] = conn
            assert place() == "placed"
    with engine.connect() as conn:
        assert conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}_orders").scalar() == 1
    assert store.purge_expired() == 0
