"""Ready-made transient database error predicates, exercised with driver-shaped fakes."""

from __future__ import annotations

import pytest

from python_wrap import retry
from python_wrap.predicates import (
    db_transient,
    error_code,
    mysql_transient,
    postgres_transient,
    sqlserver_transient,
)


def driver_error(module: str, *args: object, **attrs: object) -> Exception:
    """An exception whose class lives in ``module``, like a real driver's would."""
    cls = type("OperationalError", (Exception,), {"__module__": module})
    exc = cls(*args)
    for name, value in attrs.items():
        setattr(exc, name, value)
    return exc


class SQLAlchemyError(Exception):
    """Mimics sqlalchemy.exc.DBAPIError: the driver error is on .orig."""

    def __init__(self, orig: BaseException | None, invalidated: bool = False) -> None:
        super().__init__("wrapped")
        self.orig = orig
        self.connection_invalidated = invalidated


@pytest.mark.parametrize(
    "exc",
    [
        driver_error("psycopg.errors", "x", sqlstate="40P01"),  # deadlock
        driver_error("psycopg2.errors", "x", pgcode="40001"),  # serialization
        driver_error("asyncpg.exceptions", "x", sqlstate="08006"),  # connection failure
        driver_error("asyncpg.exceptions", "x", sqlstate="57P01"),  # admin shutdown
        driver_error("psycopg", "x", sqlstate="53300"),  # too many connections
    ],
)
def test_postgres_transient_matches(exc):
    assert postgres_transient(exc)
    assert db_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        driver_error("psycopg.errors", "x", sqlstate="23505"),  # unique violation
        driver_error("psycopg.errors", "x", sqlstate="42P01"),  # undefined table
        ValueError("plain"),
    ],
)
def test_postgres_permanent_errors_do_not_match(exc):
    assert not postgres_transient(exc)
    assert not db_transient(exc)


@pytest.mark.parametrize("code", [1205, 1213, 2006, 2013, 2003, 1040, 4031])
def test_mysql_transient_numbers(code):
    exc = driver_error("pymysql.err", code, "message")
    assert mysql_transient(exc)
    assert db_transient(exc)


@pytest.mark.parametrize("code", [1062, 1146, 1064])
def test_mysql_permanent_numbers(code):
    exc = driver_error("MySQLdb", code, "duplicate / missing table / syntax")
    assert not mysql_transient(exc)
    assert not db_transient(exc)


def test_mysql_does_not_parse_numbers_out_of_free_text():
    assert not mysql_transient(ValueError("row (2013) was bad"))


@pytest.mark.parametrize(
    "exc",
    [
        driver_error("pyodbc", "08S01", "[08S01] TCP Provider: (10054) (SQLExecDirectW)"),
        driver_error(
            "pyodbc",
            "40001",
            "[40001] Transaction was deadlocked. (1205)",
        ),
        driver_error("pyodbc", "42000", "[42000] Database 'db' on server is not available (40613)"),
        driver_error("pyodbc", "HYT00", "[HYT00] Query timeout expired (0) (SQLExecDirectW)"),
        driver_error("pymssql", 1205, b"deadlock"),
        driver_error("pymssql._mssql", 40197, b"service error"),
    ],
)
def test_sqlserver_transient_matches(exc):
    assert sqlserver_transient(exc)
    assert db_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        driver_error("pyodbc", "23000", "[23000] Violation of PRIMARY KEY constraint (2627)"),
        driver_error("pyodbc", "42S02", "[42S02] Invalid object name 'x'. (208)"),
        driver_error("pymssql", 2627, b"duplicate"),
    ],
)
def test_sqlserver_permanent_errors(exc):
    assert not sqlserver_transient(exc)
    assert not db_transient(exc)


def test_sqlserver_numbers_do_not_leak_into_mysql_and_back():
    # 1205 is "lock wait timeout" on MySQL and "deadlock victim" on SQL Server: each
    # predicate only trusts it from its own drivers.
    assert not sqlserver_transient(driver_error("pymysql.err", 1062, "dup"))
    assert not mysql_transient(driver_error("pymssql", 2013, b"not a mysql error"))


def test_sqlalchemy_orig_and_cause_chain_are_followed():
    deadlock = driver_error("pymysql.err", 1213, "Deadlock found")
    assert mysql_transient(SQLAlchemyError(deadlock))
    try:
        try:
            raise driver_error("psycopg", "x", sqlstate="40001")
        except Exception as inner:
            raise RuntimeError("repository failed") from inner
    except RuntimeError as outer:
        assert postgres_transient(outer)


def test_sqlalchemy_invalidated_connection_is_transient():
    assert db_transient(SQLAlchemyError(None, invalidated=True))
    assert not db_transient(SQLAlchemyError(None, invalidated=False))


def test_error_code_matches_structured_and_odbc_numbers():
    assert error_code(1213)(driver_error("pymysql", 1213, "x"))
    assert error_code(1205)(driver_error("pyodbc", "40001", "victim (1205)"))
    assert not error_code(1205)(ValueError("text mentioning (1205)"))
    with pytest.raises(ValueError):
        error_code()


def test_predicates_compose_and_drive_retry(fake_clock):
    attempts = []

    @retry(max_attempts=3, delay=1, retry_if=db_transient)
    def save() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise SQLAlchemyError(driver_error("pyodbc", "40001", "deadlocked (1205)"))
        return "saved"

    assert save() == "saved"
    assert len(attempts) == 3

    @retry(max_attempts=3, delay=1, retry_if=db_transient)
    def insert() -> None:
        attempts.append(1)
        raise SQLAlchemyError(driver_error("pyodbc", "23000", "PRIMARY KEY (2627)"))

    attempts.clear()
    with pytest.raises(SQLAlchemyError):
        insert()
    assert len(attempts) == 1
    combined = (postgres_transient | mysql_transient) & ~sqlserver_transient
    assert combined(driver_error("pymysql", 2006, "gone away"))
