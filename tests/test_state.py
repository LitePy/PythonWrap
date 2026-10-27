from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Any

import pytest

from python_wrap.state import Transaction, current_transaction, revert_on_failure, transactional


class Box:
    def __init__(self) -> None:
        self.n = 1
        self.items: list[Any] = [1]

    @revert_on_failure
    def fail_after_change(self) -> None:
        self.n = 99
        self.extra = True
        self.items.append(2)
        raise RuntimeError("boom")

    @revert_on_failure
    def ok(self) -> None:
        self.n = 2

    @revert_on_failure(deep=True)
    def fail_nested_deep(self) -> None:
        self.items[0].append("x")
        raise RuntimeError

    @revert_on_failure
    def fail_nested_shallow(self) -> None:
        self.items[0].append("x")
        raise RuntimeError


def test_revert_restores_values_and_removes_new_attrs() -> None:
    b = Box()
    with pytest.raises(RuntimeError):
        b.fail_after_change()
    assert b.n == 1
    assert b.items == [1]
    assert not hasattr(b, "extra")


def test_success_keeps_changes() -> None:
    b = Box()
    b.ok()
    assert b.n == 2


def test_shallow_vs_deep_semantics() -> None:
    b = Box()
    original = b.items
    with pytest.raises(RuntimeError):
        b.fail_after_change()
    # contents reverted, but the attribute now holds a copy, not the old object
    assert b.items == [1]
    assert b.items is not original
    assert original == [1, 2]

    b = Box()
    b.items = [[]]
    with pytest.raises(RuntimeError):
        b.fail_nested_shallow()
    assert b.items == [["x"]]  # nested element mutation not reverted

    b = Box()
    b.items = [[]]
    with pytest.raises(RuntimeError):
        b.fail_nested_deep()
    assert b.items == [[]]


def test_target_by_name_and_index() -> None:
    class Obj:
        v = 0

    @revert_on_failure(target="o")
    def by_name(x: int, o: Obj) -> None:
        o.v = x
        raise ValueError

    @revert_on_failure(target=1)
    def by_index(x: int, o: Obj) -> None:
        o.v = x
        raise ValueError

    o = Obj()
    o.v = 5
    for fn in (by_name, by_index):
        with pytest.raises(ValueError):
            fn(1, o)
        assert o.v == 5
    with pytest.raises(ValueError):
        by_name(x=1, o=o)
    assert o.v == 5
    with pytest.raises(ValueError, match="no parameter"):
        revert_on_failure(target="missing")(by_name.__wrapped__)  # type: ignore[attr-defined]


def test_attrs_with_slots() -> None:
    class Slotted:
        __slots__ = ("a", "b")

        def __init__(self) -> None:
            self.a = 1

        @revert_on_failure(attrs=["a", "b"])
        def go(self) -> None:
            self.a = 2
            self.b = 3
            raise RuntimeError

        @revert_on_failure
        def no_attrs(self) -> None: ...

    s = Slotted()
    with pytest.raises(RuntimeError):
        s.go()
    assert s.a == 1
    assert not hasattr(s, "b")
    with pytest.raises(TypeError, match="attrs="):
        s.no_attrs()


def test_attrs_subset_leaves_others() -> None:
    class C:
        def __init__(self) -> None:
            self.a = 1
            self.b = 1

        @revert_on_failure(attrs=("a",))
        def go(self) -> None:
            self.a = 2
            self.b = 2
            raise RuntimeError

    c = C()
    with pytest.raises(RuntimeError):
        c.go()
    assert (c.a, c.b) == (1, 2)


def test_uncopyable_value_restored_by_reference() -> None:
    class C:
        def __init__(self) -> None:
            self.lock = threading.Lock()

        @revert_on_failure(deep=True)
        def go(self) -> None:
            self.lock = None  # type: ignore[assignment]
            raise RuntimeError

    c = C()
    lock = c.lock
    with pytest.raises(RuntimeError):
        c.go()
    assert c.lock is lock


def test_bad_call_raises_function_type_error() -> None:
    @revert_on_failure
    def f(o: object) -> None: ...

    with pytest.raises(TypeError):
        f()  # type: ignore[call-arg]


async def test_revert_async() -> None:
    class C:
        n = 0

        @revert_on_failure
        async def go(self) -> None:
            self.n = 5
            await asyncio.sleep(0)
            raise RuntimeError

    c = C()
    assert inspect.iscoroutinefunction(C.go)
    with pytest.raises(RuntimeError):
        await c.go()
    assert c.n == 0


# ------------------------------------------------------------------------- Transaction


def test_commit_runs_in_order() -> None:
    out: list[str] = []
    with Transaction() as tx:
        tx.on_commit(out.append, "a")
        tx.on_commit(out.append, "b")
        tx.on_rollback(out.append, "r")
    assert out == ["a", "b"]
    assert tx.state == "committed"


def test_rollback_reverse_and_continues_after_failure(caplog: pytest.LogCaptureFixture) -> None:
    out: list[str] = []

    def bad() -> None:
        raise OSError("nope")

    with (
        caplog.at_level(logging.ERROR, logger="python_wrap"),
        pytest.raises(ValueError, match="orig"),
        Transaction() as tx,
    ):
        tx.on_rollback(out.append, 1)
        tx.on_rollback(bad)
        tx.on_rollback(out.append, 3)
        tx.on_commit(out.append, "never")
        raise ValueError("orig")
    assert out == [3, 1]
    assert tx.state == "rolled_back"
    rec = [r for r in caplog.records if "rollback callback" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_completes_only_once() -> None:
    tx = Transaction()
    tx.commit()
    with pytest.raises(RuntimeError):
        tx.rollback()
    with pytest.raises(RuntimeError):
        tx.commit()
    with pytest.raises(RuntimeError):
        tx.on_commit(print)
    with pytest.raises(RuntimeError):
        tx.on_rollback(print)

    tx2 = Transaction()
    with tx2:
        tx2.rollback()  # manual completion; exit does nothing
    assert tx2.state == "rolled_back"


def test_explicit_rollback_and_kwargs() -> None:
    out: dict[str, int] = {}
    tx = Transaction()
    tx.on_rollback(out.update, a=1)
    tx.rollback()
    assert out == {"a": 1}


def test_sync_rejects_async_callback() -> None:
    async def cb() -> None: ...

    tx = Transaction()
    tx.on_commit(cb)
    with pytest.raises(TypeError, match="async"):
        tx.commit()

    tx = Transaction()
    tx.on_rollback(cb)
    tx.rollback()  # logged, not raised


def test_commit_callback_error_propagates() -> None:
    def bad() -> None:
        raise KeyError

    with pytest.raises(KeyError), Transaction() as tx:
        tx.on_commit(bad)


async def test_async_transaction() -> None:
    out: list[str] = []

    async def acb(v: str) -> None:
        await asyncio.sleep(0)
        out.append(v)

    async with Transaction() as tx:
        tx.on_commit(acb, "c1")
        tx.on_commit(out.append, "c2")
    assert out == ["c1", "c2"]

    out.clear()

    async def bad() -> None:
        raise RuntimeError

    with pytest.raises(ValueError):
        async with Transaction() as tx:
            tx.on_rollback(acb, "r1")
            tx.on_rollback(bad)
            tx.on_rollback(acb, "r3")
            raise ValueError
    assert out == ["r3", "r1"]

    async with Transaction() as tx2:
        await tx2.acommit()
    assert tx2.state == "committed"


# ----------------------------------------------------------------------- transactional


def test_transactional_tx_param_and_current() -> None:
    out: list[str] = []

    @transactional
    def work(a: int, tx: Transaction, fail: bool = False) -> int:
        assert current_transaction() is tx
        tx.on_commit(out.append, "commit")
        tx.on_rollback(out.append, "rollback")
        if fail:
            raise ValueError
        return a

    assert work(1) == 1
    assert out == ["commit"]
    with pytest.raises(ValueError):
        work(1, fail=True)  # type: ignore[call-arg]
    assert out == ["commit", "rollback"]
    assert current_transaction() is None


def test_transactional_without_tx_param_and_nesting() -> None:
    seen: list[Transaction | None] = []

    @transactional(logger="x")
    def inner() -> None:
        tx = current_transaction()
        assert tx is not None
        seen.append(tx)
        tx.on_rollback(seen.append, "inner-rb")

    @transactional
    def outer() -> None:
        tx = current_transaction()
        seen.append(tx)
        inner()
        assert current_transaction() is tx
        raise RuntimeError

    with pytest.raises(RuntimeError):
        outer()
    assert seen[0] is not seen[1]
    assert "inner-rb" not in seen  # inner committed on its own


def test_transactional_thread_isolation() -> None:
    results: dict[str, Transaction | None] = {}

    @transactional
    def work(name: str) -> None:
        results[name] = current_transaction()

    ts = [threading.Thread(target=work, args=(str(i),)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(v) for v in results.values()}) == 3
    assert current_transaction() is None


async def test_transactional_async() -> None:
    out: list[str] = []

    async def undo() -> None:
        out.append("undo")

    @transactional
    async def work(fail: bool, *, tx: Transaction) -> str:
        tx.on_rollback(undo)
        await asyncio.sleep(0)
        assert current_transaction() is tx
        if fail:
            raise RuntimeError
        return "ok"

    assert inspect.iscoroutinefunction(work)
    assert await work(False) == "ok"
    assert out == []
    with pytest.raises(RuntimeError):
        await work(True)
    assert out == ["undo"]
    assert current_transaction() is None
    results = await asyncio.gather(work(False), work(False))
    assert results == ["ok", "ok"]


def test_revert_on_classmethod_target_keeps_original_exception() -> None:
    class Config:
        value = 1

        @classmethod
        @revert_on_failure
        def update(cls, v: int) -> None:
            cls.value = v
            cls.extra = v  # type: ignore[attr-defined]
            raise ValueError("boom")

        @classmethod
        def helper(cls) -> int:
            return 7

    with pytest.raises(ValueError, match="boom"):
        Config.update(5)
    assert Config.value == 1
    assert not hasattr(Config, "extra")
    assert Config.helper() == 7


def test_restore_failure_is_logged_and_original_exception_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Guard:
        def __init__(self) -> None:
            object.__setattr__(self, "a", 1)

        def __setattr__(self, name: str, value: Any) -> None:
            raise RuntimeError("frozen")

    @revert_on_failure
    def f(g: Guard) -> None:
        object.__setattr__(g, "a", 2)
        raise ValueError("orig")

    g = Guard()
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="orig"):
        f(g)
    assert any("could not" in r.getMessage() for r in caplog.records)


def test_injected_tx_typed_default_and_explicit_tx_rejected() -> None:
    from python_wrap.state import INJECTED, require_transaction

    seen: list[Transaction] = []

    @transactional
    def charge(amount: int, *, tx: Transaction = INJECTED) -> int:
        seen.append(tx)
        assert require_transaction() is tx
        return amount

    assert charge(5) == 5
    assert isinstance(seen[0], Transaction) and seen[0].state == "committed"
    with pytest.raises(TypeError, match="do not pass tx"):
        charge(5, tx=Transaction())
    with pytest.raises(TypeError, match="do not pass tx"):

        @transactional
        def positional(tx: Transaction = INJECTED) -> None: ...

        positional(Transaction())
    assert repr(INJECTED) == "INJECTED"


async def test_injected_tx_async_and_explicit_rejected() -> None:
    from python_wrap.state import INJECTED

    @transactional
    async def charge(*, tx: Transaction = INJECTED) -> Transaction:
        return tx

    assert (await charge()).state == "committed"
    with pytest.raises(TypeError):
        await charge(tx=Transaction())


def test_tx_that_cannot_be_injected_by_keyword_is_rejected_at_decoration() -> None:
    from python_wrap.state import INJECTED

    def posonly(tx: Transaction = INJECTED, /) -> None: ...
    def star(*tx: Any) -> None: ...
    def starstar(**tx: Any) -> None: ...

    for func, kind in ((posonly, "positional-only"), (star, "variadic"), (starstar, "variadic")):
        with pytest.raises(TypeError, match=f"cannot inject 'tx'.*{kind}"):
            transactional(func)

    @transactional
    def before_star(a: int, tx: Transaction = INJECTED, *rest: int) -> str:
        return tx.state

    assert before_star(1) == "pending"  # still open while the body runs


def test_require_transaction_outside_raises() -> None:
    from python_wrap.state import require_transaction

    with pytest.raises(RuntimeError, match="no active transaction"):
        require_transaction()
