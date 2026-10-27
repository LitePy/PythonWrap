"""State safety: `revert_on_failure` and transactions."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import inspect
import threading
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Final, ParamSpec, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import F, LoggerLike

__all__ = [
    "INJECTED",
    "Transaction",
    "current_transaction",
    "require_transaction",
    "revert_on_failure",
    "transactional",
]

P = ParamSpec("P")
R = TypeVar("R")

_NO_VALUE: Any = object()


# ------------------------------------------------------------------ revert_on_failure


def _copy_value(value: Any, deep: bool) -> Any:
    try:
        return copy.deepcopy(value) if deep else copy.copy(value)
    except Exception:
        return value  # not copyable (locks, sockets...): restored by reference


class _Snapshot:
    def __init__(self, obj: Any, attrs: tuple[str, ...] | None, deep: bool) -> None:
        self.obj = obj
        self.attrs = attrs
        self.values: dict[str, Any] = {}
        if attrs is None:
            try:
                current = vars(obj)
            except TypeError:
                raise TypeError(
                    f"revert_on_failure: {type(obj).__name__!r} has no __dict__ "
                    "(it uses __slots__?); pass attrs=[...] to name the attributes"
                ) from None
            names: Iterable[str] = [n for n in current if self._wanted(n)]
        else:
            names = attrs
        for name in names:
            try:
                # Classes: take the raw entry so classmethods/properties stay descriptors.
                value = (
                    vars(obj)[name]
                    if isinstance(obj, type) and name in vars(obj)
                    else getattr(obj, name)
                )
            except AttributeError:
                self.values[name] = _NO_VALUE
            else:
                self.values[name] = _copy_value(value, deep)

    def _wanted(self, name: str) -> bool:
        # Classes: leave dunders (``__dict__``, ``__weakref__``, ``__module__``...) alone.
        return not (isinstance(self.obj, type) and name.startswith("__") and name.endswith("__"))

    def restore(self) -> None:
        """Put the attributes back; never raises (failures are logged)."""
        obj = self.obj
        log = _core.get_logger(None)
        if self.attrs is None:
            try:
                extra = [n for n in vars(obj) if n not in self.values and self._wanted(n)]
            except Exception:
                extra = []
            for name in extra:
                try:
                    delattr(obj, name)
                except Exception:
                    log.warning("revert_on_failure: could not delete %r", name, exc_info=True)
        for name, value in self.values.items():
            try:
                if value is _NO_VALUE:
                    with contextlib.suppress(AttributeError):
                        delattr(obj, name)
                else:
                    setattr(obj, name, value)
            except Exception:
                log.warning("revert_on_failure: could not restore %r", name, exc_info=True)


def _target_resolver(
    func: Callable[..., Any], target: str | int
) -> Callable[[tuple[Any, ...], dict[str, Any]], Any]:
    sig = inspect.signature(func)
    if isinstance(target, str):
        if target not in sig.parameters:
            raise ValueError(f"revert_on_failure: {func!r} has no parameter {target!r}")

        def by_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            return sig.bind(*args, **kwargs).arguments[target]

        return by_name

    def by_index(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args[target]

    return by_index


@overload
def revert_on_failure(func: F, /) -> F: ...
@overload
def revert_on_failure(
    func: None = None,
    /,
    *,
    target: str | int = 0,
    attrs: Iterable[str] | None = None,
    deep: bool = False,
) -> Callable[[F], F]: ...
def revert_on_failure(
    func: Callable[..., Any] | None = None,
    /,
    *,
    target: str | int = 0,
    attrs: Iterable[str] | None = None,
    deep: bool = False,
) -> Any:
    """Restore an object's attributes if the function raises.

    Before the call, the attributes of the argument ``target`` (a positional index or a
    parameter name; the first argument, normally ``self``, by default) are snapshotted:
    all of ``vars(obj)``, or only ``attrs`` (required for ``__slots__`` classes). If the
    function raises, every snapshotted attribute is set back, attributes created during
    the call are deleted (when snapshotting all of ``vars``; with ``attrs`` an attribute
    that did not exist before is deleted), and the exception is re-raised.

    Copy semantics: each value is copied with ``copy.copy`` (``copy.deepcopy`` with
    ``deep=True``) and the *copy* is what gets restored. Therefore in-place mutation of
    a container attribute (``self.items.append(x)``) IS undone, but the attribute ends
    up referring to a new list object, so other references to the old list still see
    the mutation. A shallow copy does not protect the elements: mutating an object
    nested inside the list (``self.items[0].count += 1``) is only undone with
    ``deep=True``. Values that cannot be copied are restored by reference.

    v0.1 reported to revert but its snapshot was always empty, so nothing was restored.

    Args:
        func: The function (when used bare).
        target: Index or name of the argument whose attributes are protected.
        attrs: Restrict the snapshot to these attribute names.
        deep: Use ``copy.deepcopy`` for the snapshot.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        ValueError: If ``target`` names a parameter the function does not have.
        TypeError: At call time, if no ``attrs`` are given and the object has no
            ``__dict__``.

    Example:
        >>> class Account:
        ...     def __init__(self):
        ...         self.balance = 10
        ...     @revert_on_failure
        ...     def withdraw(self, n):
        ...         self.balance -= n
        ...         if self.balance < 0:
        ...             raise ValueError("overdrawn")
    """
    attr_names = None if attrs is None else tuple(attrs)

    def decorate(f: Callable[..., Any]) -> Any:
        resolve = _target_resolver(f, target)

        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> _Snapshot | None:
            try:
                obj = resolve(args, kwargs)
            except (IndexError, KeyError, TypeError):
                return None  # bad call: let the function raise its own TypeError
            return _Snapshot(obj, attr_names, deep)

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                snap = begin(args, kwargs)
                try:
                    return await f(*args, **kwargs)
                except BaseException:
                    if snap is not None:
                        snap.restore()
                    raise

            return awrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            snap = begin(args, kwargs)
            try:
                return f(*args, **kwargs)
            except BaseException:
                if snap is not None:
                    snap.restore()
                raise

        return wrapper

    return decorate if func is None else decorate(func)


# --------------------------------------------------------------------- transactions

_current: contextvars.ContextVar[Transaction | None] = contextvars.ContextVar(
    "python_wrap_current_transaction", default=None
)


def current_transaction() -> Transaction | None:
    """Return the transaction of the innermost running ``@transactional`` call, if any."""
    return _current.get()


def require_transaction() -> Transaction:
    """Return the current transaction, raising if there is none.

    Like `current_transaction` but typed as `Transaction`, so there is no
    ``assert tx is not None`` boilerplate.

    Returns:
        The transaction of the innermost running ``@transactional`` call.

    Raises:
        RuntimeError: If called outside a ``@transactional`` function.

    Example:
        >>> @transactional
        ... def charge():
        ...     require_transaction().on_rollback(print, "undo")
    """
    tx = _current.get()
    if tx is None:
        raise RuntimeError("no active transaction: call this from a @transactional function")
    return tx


class Transaction:
    """Collects commit and rollback callbacks and runs the right set once.

    Use as a context manager (``with`` or ``async with``). On exception the rollback
    callbacks run in reverse registration order; a failing one is logged and the rest
    still run; then the original exception propagates. On success the commit callbacks
    run in order (an exception from one propagates and skips the remaining ones).
    A transaction completes only once: a second ``commit()``/``rollback()`` raises
    ``RuntimeError``. Callbacks that are coroutine functions need ``async with`` (or
    ``await tx.acommit()`` / ``await tx.arollback()``).

    Args:
        logger: Logger (or name) used to report failing rollback callbacks.

    Example:
        >>> with Transaction() as tx:
        ...     tx.on_rollback(print, "undo")
    """

    def __init__(self, logger: LoggerLike = None) -> None:
        self._logger = _core.get_logger(logger)
        self._commit: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []
        self._rollback: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []
        self._state = "pending"
        self._state_lock = threading.Lock()

    @property
    def state(self) -> str:
        """``"pending"``, ``"committed"`` or ``"rolled_back"``."""
        return self._state

    def _check_open(self) -> None:
        if self._state != "pending":
            raise RuntimeError(f"transaction already {self._state}")

    def _complete(self, state: str) -> None:
        """Atomically move from pending to ``state``; only one caller can succeed."""
        with self._state_lock:
            self._check_open()
            self._state = state

    def on_rollback(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Register ``fn(*args, **kwargs)`` to run if the transaction rolls back."""
        self._check_open()
        self._rollback.append((fn, args, kwargs))

    def on_commit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Register ``fn(*args, **kwargs)`` to run if the transaction commits."""
        self._check_open()
        self._commit.append((fn, args, kwargs))

    @staticmethod
    def _call_sync(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError(
                f"transaction callback {_core.qualname(fn)} is async; "
                "use 'async with' or await acommit()/arollback()"
            )

    def commit(self) -> None:
        """Mark committed and run the commit callbacks in order."""
        self._complete("committed")
        for fn, args, kwargs in self._commit:
            self._call_sync(fn, args, kwargs)

    def rollback(self) -> None:
        """Mark rolled back and run the rollback callbacks in reverse order."""
        self._complete("rolled_back")
        for fn, args, kwargs in reversed(self._rollback):
            try:
                self._call_sync(fn, args, kwargs)
            except Exception:
                self._logger.error("rollback callback %s failed", _core.qualname(fn), exc_info=True)

    async def acommit(self) -> None:
        """Like `commit`, awaiting async callbacks."""
        self._complete("committed")
        for fn, args, kwargs in self._commit:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                await result

    async def arollback(self) -> None:
        """Like `rollback`, awaiting async callbacks."""
        self._complete("rolled_back")
        for fn, args, kwargs in reversed(self._rollback):
            try:
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self._logger.error("rollback callback %s failed", _core.qualname(fn), exc_info=True)

    def __enter__(self) -> Transaction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._state != "pending":
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    async def __aenter__(self) -> Transaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._state != "pending":
            return
        if exc_type is None:
            await self.acommit()
        else:
            await self.arollback()


class _Injected:
    __slots__ = ()

    def __repr__(self) -> str:
        return "INJECTED"


INJECTED: Final[Transaction] = cast(Transaction, _Injected())
"""Typed placeholder default for a ``tx`` parameter that `transactional` fills in.

Write ``def charge(amount: int, *, tx: Transaction = INJECTED) -> None`` and callers
type-check without passing ``tx``. Never use the value itself."""


@overload
def transactional(func: F, /) -> F: ...
@overload
def transactional(func: None = None, /, *, logger: LoggerLike = None) -> Callable[[F], F]: ...
def transactional(func: Callable[..., Any] | None = None, /, *, logger: LoggerLike = None) -> Any:
    """Run the function inside a fresh `Transaction` per call.

    If the function has a parameter named ``tx`` it receives the transaction as the
    keyword argument ``tx=``. Declare it as ``tx: Transaction = INJECTED`` (keyword-only is
    cleanest) so callers type-check without passing it; passing ``tx`` yourself raises
    ``TypeError``. Otherwise use `require_transaction` / `current_transaction` (a
    ``ContextVar``, correct across threads and asyncio tasks). Returning normally
    commits, raising rolls back and re-raises. Nested ``@transactional`` calls each get their own transaction. Works for
    sync and ``async def`` functions (async callbacks are awaited).

    v0.1's ``transactional`` was a no-op.

    Args:
        func: The function (when used bare).
        logger: Logger (or name) for failing rollback callbacks.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        TypeError: At call time, if the caller passes ``tx`` explicitly.

    Example:
        >>> @transactional
        ... def transfer(tx: Transaction = INJECTED):
        ...     tx.on_rollback(print, "undone")
    """

    def decorate(f: Callable[..., Any]) -> Any:
        tx_pos: int | None = None
        try:
            params = inspect.signature(f).parameters
            wants_tx = "tx" in params
            if wants_tx:
                names = list(params)
                kind = params["tx"].kind
                if kind in (kind.POSITIONAL_ONLY, kind.POSITIONAL_OR_KEYWORD):
                    tx_pos = names.index("tx")
        except (TypeError, ValueError):
            wants_tx = False

        def check_not_passed(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if ("tx" in kwargs and kwargs["tx"] is not INJECTED) or (
                tx_pos is not None and len(args) > tx_pos and args[tx_pos] is not INJECTED
            ):
                raise TypeError(
                    f"{_core.qualname(f)}() received 'tx' from the caller, but @transactional "
                    "supplies it; do not pass tx"
                )

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                if wants_tx:
                    check_not_passed(args, kwargs)
                tx = Transaction(logger)
                if wants_tx:
                    kwargs["tx"] = tx
                token = _current.set(tx)
                try:
                    async with tx:
                        return await f(*args, **kwargs)
                finally:
                    _current.reset(token)

            return awrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if wants_tx:
                check_not_passed(args, kwargs)
            tx = Transaction(logger)
            if wants_tx:
                kwargs["tx"] = tx
            token = _current.set(tx)
            try:
                with tx:
                    return f(*args, **kwargs)
            finally:
                _current.reset(token)

        return wrapper

    return decorate if func is None else decorate(func)
