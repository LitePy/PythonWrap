"""Composable exception predicates without importing HTTP or database drivers.

Besides the building blocks (`sqlstate`, `message_contains`, `in_cause_chain`) this module
ships ready-made *transient error* predicates for the common database drivers:
`postgres_transient`, `mysql_transient`, `sqlserver_transient` and `db_transient` (their
union, also aware of SQLAlchemy's ``connection_invalidated`` flag). They inspect
attributes and error codes only, so no driver is imported.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "MYSQL_TRANSIENT_CODES",
    "POSTGRES_TRANSIENT_SQLSTATES",
    "SQLSERVER_TRANSIENT_CODES",
    "SQLSERVER_TRANSIENT_SQLSTATES",
    "Predicate",
    "db_transient",
    "error_code",
    "in_cause_chain",
    "message_contains",
    "mysql_transient",
    "postgres_transient",
    "sqlserver_transient",
    "sqlstate",
]


class Predicate:
    """A callable predicate supporting short-circuit |, &, and ~ composition."""

    def __init__(self, test: Callable[[Any], bool]) -> None:
        self.test = test

    def __call__(self, value: Any) -> bool:
        return bool(self.test(value))

    def __or__(self, other: Callable[[Any], bool]) -> Predicate:
        return Predicate(lambda value: self(value) or other(value))

    def __and__(self, other: Callable[[Any], bool]) -> Predicate:
        return Predicate(lambda value: self(value) and other(value))

    def __invert__(self) -> Predicate:
        return Predicate(lambda value: not self(value))


def message_contains(*fragments: str, case_sensitive: bool = False) -> Predicate:
    """Match any fragment in an exception's message (case-insensitive by default)."""
    if not fragments or any(not text for text in fragments):
        raise ValueError("provide nonempty message fragments")
    needles = fragments if case_sensitive else tuple(text.casefold() for text in fragments)
    return Predicate(
        lambda exc: any(
            text in (str(exc) if case_sensitive else str(exc).casefold()) for text in needles
        )
    )


def sqlstate(*prefixes: str) -> Predicate:
    """Match SQLSTATE prefixes on .sqlstate, .pgcode, or DB-API args[0]."""
    if not prefixes or any(not value for value in prefixes):
        raise ValueError("provide SQLSTATE prefixes")

    def test(exc: Any) -> bool:
        args = getattr(exc, "args", ())
        values = (
            getattr(exc, "sqlstate", None),
            getattr(exc, "pgcode", None),
            args[0] if args else None,
        )
        return any(isinstance(value, str) and value.startswith(prefixes) for value in values)

    return Predicate(test)


def in_cause_chain(test: Callable[[Any], bool]) -> Predicate:
    """Walk cause/context and driver .orig links, including cycles, once per exception."""

    def matches(exc: Any) -> bool:
        seen: set[int] = set()
        queue = [exc]
        while queue:
            current = queue.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if test(current):
                return True
            queue.extend(
                getattr(current, field, None) for field in ("__cause__", "__context__", "orig")
            )
        return False

    return Predicate(matches)


def _codes_of(exc: Any, *, parse_message: bool = False) -> set[int]:
    """Integer error numbers carried by a DB-API exception.

    MySQL drivers (PyMySQL, mysqlclient, aiomysql, asyncmy) put the server error number
    in ``args[0]``; pymssql uses ``args[0]`` or ``.number``; pyodbc only has an ODBC
    SQLSTATE in ``args[0]`` and the native SQL Server number in parentheses inside the
    message, as in ``"... Deadlock victim ... (1205) (SQLExecDirectW)"``.
    """
    found: set[int] = set()
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        found.add(args[0])
    for field in ("errno", "number", "code"):
        value = getattr(exc, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            found.add(value)
    if not parse_message:
        return found
    for text in (item for item in args if isinstance(item, str)):
        found.update(int(code) for code in _NATIVE_CODE.findall(text))
    return found


_NATIVE_CODE = re.compile(r"\((\d{1,6})\)")


def error_code(*codes: int) -> Predicate:
    """Match driver error numbers (``args[0]``, ``.errno``, ``.number``, ``(1205)`` in text).

    Args:
        *codes: Integer server error numbers, such as MySQL 1213 or SQL Server 1205.

    Returns:
        A `Predicate` true when any of the codes is present on the exception itself.

    Raises:
        ValueError: If no code is given.

    Example:
        >>> class OperationalError(Exception): ...
        >>> error_code(1213)(OperationalError(1213, "Deadlock found"))
        True
    """
    if not codes:
        raise ValueError("provide at least one error code")
    wanted = frozenset(codes)
    return Predicate(lambda exc: bool(_codes_of(exc, parse_message=_odbc_style(exc)) & wanted))


#: SQLSTATE prefixes worth retrying on PostgreSQL: connection exceptions (08),
#: serialization failure and deadlock (40001, 40P01), operator intervention such as
#: admin shutdown or a server still starting (57P01-57P03), too many connections
#: (53300) and lock_not_available (55P03).
POSTGRES_TRANSIENT_SQLSTATES: tuple[str, ...] = (
    "08",
    "40001",
    "40P01",
    "53300",
    "55P03",
    "57P01",
    "57P02",
    "57P03",
)

#: MySQL/MariaDB server and client error numbers worth retrying: lock wait timeout
#: (1205), deadlock (1213), too many connections (1040), connection/host errors
#: (2002, 2003, 2006 server gone away, 2013 lost connection, 2055), the 8.0 idle
#: disconnect (4031), read-only during failover (1290, 1836) and query interrupted by
#: a killed connection (1927).
MYSQL_TRANSIENT_CODES: frozenset[int] = frozenset(
    {1040, 1205, 1213, 1290, 1836, 1927, 2002, 2003, 2006, 2013, 2055, 4031}
)

#: ODBC SQLSTATEs reported by pyodbc for SQL Server that are worth retrying:
#: communication link failures (08xxx), serialization failure / deadlock (40001) and
#: timeout expired (HYT00, HYT01).
SQLSERVER_TRANSIENT_SQLSTATES: tuple[str, ...] = ("08", "40001", "HYT00", "HYT01")

#: SQL Server and Azure SQL native error numbers documented as transient: deadlock
#: victim (1205), lock request timeout (1222), Azure SQL database unavailable or
#: reconfiguring (40613, 40197, 40501, 40540, 40544, 40549, 40550, 40551, 40552,
#: 40553), resource limits (10928, 10929), elastic pool busy (49918, 49919, 49920),
#: login failures during failover (4060, 4221) and transport
#: errors (64, 233, 10053, 10054, 10060, 10061).
SQLSERVER_TRANSIENT_CODES: frozenset[int] = frozenset(
    {
        64,
        233,
        1205,
        1222,
        4060,
        4221,
        10053,
        10054,
        10060,
        10061,
        10928,
        10929,
        40197,
        40501,
        40540,
        40544,
        40549,
        40550,
        40551,
        40552,
        40553,
        40613,
        49918,
        49919,
        49920,
    }
)


def _has_sqlstate(exc: Any, prefixes: tuple[str, ...]) -> bool:
    return sqlstate(*prefixes)(exc)


def _is_mysql(exc: Any) -> bool:
    module = type(exc).__module__.split(".")[0]
    return module in {"pymysql", "MySQLdb", "aiomysql", "asyncmy", "mysql", "mariadb"}


def _is_sqlserver(exc: Any) -> bool:
    module = type(exc).__module__.split(".")[0]
    return module in {"pyodbc", "pymssql", "_mssql", "aioodbc", "mssql_python"}


def _pg(exc: Any) -> bool:
    return _has_sqlstate(exc, POSTGRES_TRANSIENT_SQLSTATES) and not _is_sqlserver(exc)


def _mysql(exc: Any) -> bool:
    return not _is_sqlserver(exc) and bool(_codes_of(exc) & MYSQL_TRANSIENT_CODES)


def _odbc_style(exc: Any) -> bool:
    """True for SQL Server driver errors, or any error whose args[0] is a SQLSTATE."""
    args = getattr(exc, "args", ())
    first = args[0] if args else None
    return _is_sqlserver(exc) or (
        isinstance(first, str) and len(first) == 5 and first.isalnum() and first.isupper()
    )


def _sqlserver(exc: Any) -> bool:
    if _is_mysql(exc):
        return False
    if _has_sqlstate(exc, SQLSERVER_TRANSIENT_SQLSTATES):
        return True
    codes = _codes_of(exc, parse_message=_odbc_style(exc))
    return bool(codes & SQLSERVER_TRANSIENT_CODES)


def _invalidated(exc: Any) -> bool:
    return getattr(exc, "connection_invalidated", False) is True


#: Transient PostgreSQL errors (psycopg 2/3 ``.pgcode``/``.sqlstate``, asyncpg
#: ``.sqlstate``), searched through the cause chain and SQLAlchemy's ``.orig``.
postgres_transient: Predicate = in_cause_chain(_pg)

#: Transient MySQL/MariaDB errors by server error number, through the cause chain and
#: SQLAlchemy's ``.orig``. Numbers on exceptions from SQL Server drivers are ignored.
mysql_transient: Predicate = in_cause_chain(_mysql)

#: Transient SQL Server / Azure SQL errors (pyodbc SQLSTATE and native numbers in the
#: message, pymssql numbers), through the cause chain and SQLAlchemy's ``.orig``.
sqlserver_transient: Predicate = in_cause_chain(_sqlserver)

#: Any of the above, or a SQLAlchemy error whose ``connection_invalidated`` is True.
db_transient: Predicate = in_cause_chain(
    lambda exc: _invalidated(exc) or _pg(exc) or _mysql(exc) or _sqlserver(exc)
)
