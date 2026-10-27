# State safety

Keep objects consistent when something goes wrong halfway.

## `revert_on_failure`

If the function raises, restore the object's attributes to what they were before the call, then re-raise.

```python
from python_wrap import revert_on_failure

class Account:
    def __init__(self) -> None:
        self.balance = 10
        self.history: list[str] = []

    @revert_on_failure
    def withdraw(self, amount: int) -> None:
        self.history.append(f"withdraw {amount}")
        self.balance -= amount
        if self.balance < 0:
            raise ValueError("overdrawn")

account = Account()
try:
    account.withdraw(50)
except ValueError:
    pass

assert account.balance == 10      # restored
assert account.history == []      # the append was undone too
```

By default it snapshots `vars(self)`: the first argument of the call. Attributes created during the call are deleted again on failure.

| Option | Meaning |
| --- | --- |
| `target` | Index or parameter name of the argument to protect (default `0`, normally `self`). |
| `attrs` | Snapshot only these attribute names. **Required for classes using `__slots__`.** |
| `deep` | Use `copy.deepcopy` for the snapshot instead of `copy.copy`. |

```python
from python_wrap import revert_on_failure

class Cart:
    def __init__(self) -> None:
        self.items: list[dict] = [{"qty": 1}]
        self.coupon = None

    @revert_on_failure(attrs=["items"], deep=True)
    def bump(self) -> None:
        self.items[0]["qty"] += 1   # a nested object
        self.coupon = "TEN"         # not protected: not in attrs
        raise RuntimeError

cart = Cart()
try:
    cart.bump()
except RuntimeError:
    pass

assert cart.items[0]["qty"] == 1    # deep=True undoes nested mutation
assert cart.coupon == "TEN"         # outside attrs, so not restored
```

!!! warning "Copy semantics"
    Each value is copied (`copy.copy`, or `copy.deepcopy` with `deep=True`) and the **copy** is restored. In-place mutation of a container attribute (`self.items.append(x)`) is undone, but the attribute ends up referring to a new list, so other references to the old list still see the mutation. A shallow copy does not protect nested objects: `self.items[0].count += 1` is only undone with `deep=True`. Values that cannot be copied are restored by reference. See [Limitations](../limitations.md#revert_on_failure).

It works on `async def` methods too. It undoes attribute changes only, not side effects such as files written or requests sent; for those, use a transaction.

## Transactions

A `Transaction` collects undo steps (`on_rollback`) and follow-up steps (`on_commit`) and runs the right set exactly once.

- On success, `on_commit` callbacks run **in order**.
- On exception, `on_rollback` callbacks run **in reverse registration order**; a failing rollback callback is logged and the rest still run; then the original exception propagates.
- A transaction completes once; a second `commit()` or `rollback()` raises `RuntimeError`.

=== "Sync"

    ```python
    from python_wrap import Transaction

    log: list[str] = []

    try:
        with Transaction() as tx:
            log.append("created file")
            tx.on_rollback(log.append, "removed file")
            log.append("charged card")
            tx.on_rollback(log.append, "refunded card")
            tx.on_commit(log.append, "sent receipt")
            raise RuntimeError("shipping failed")
    except RuntimeError:
        pass

    assert tx.state == "rolled_back"
    assert log == ["created file", "charged card", "refunded card", "removed file"]
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import Transaction

    log: list[str] = []

    async def undo() -> None:
        log.append("undone")

    async def main() -> None:
        try:
            async with Transaction() as tx:
                tx.on_rollback(undo)          # coroutine callbacks need `async with`
                raise RuntimeError
        except RuntimeError:
            pass
        assert log == ["undone"]

    asyncio.run(main())
    ```

`tx.state` is `"pending"`, `"committed"` or `"rolled_back"`. Outside a context manager you can call `tx.commit()` / `tx.rollback()`, or `await tx.acommit()` / `await tx.arollback()` when callbacks are coroutine functions.

## `transactional`

Wrap a function in a fresh `Transaction` per call. Returning normally commits; raising rolls back and re-raises. Receive the transaction as a keyword parameter named `tx` declared as `tx: Transaction = INJECTED`; `INJECTED` is a typed placeholder, so type checkers let callers omit `tx` (passing it yourself raises `TypeError`). Or fetch it anywhere in the call tree with `require_transaction()`, which raises `RuntimeError` outside a transaction, or `current_transaction()`, which returns `None` there.

=== "Sync"

    ```python
    from python_wrap.state import INJECTED, Transaction, transactional

    steps: list[str] = []

    @transactional
    def transfer(amount: int, *, tx: Transaction = INJECTED) -> str:
        steps.append("debit")
        tx.on_rollback(steps.append, "undo debit")
        if amount > 100:
            raise ValueError("limit")
        tx.on_commit(steps.append, "notify")
        return "ok"

    assert transfer(50) == "ok"
    assert steps == ["debit", "notify"]

    steps.clear()
    try:
        transfer(500)
    except ValueError:
        pass
    assert steps == ["debit", "undo debit"]
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap.state import INJECTED, Transaction, transactional

    steps: list[str] = []

    @transactional
    async def transfer(amount: int, *, tx: Transaction = INJECTED) -> str:
        steps.append("debit")
        tx.on_rollback(steps.append, "undo debit")
        if amount > 100:
            raise ValueError("limit")
        return "ok"

    async def main() -> None:
        try:
            await transfer(500)
        except ValueError:
            pass
        assert steps == ["debit", "undo debit"]

    asyncio.run(main())
    ```

!!! warning "Make `tx` keyword-only"
    When the function has a parameter named `tx`, it is passed as the keyword argument `tx=`. Callers must not pass it themselves (a `TypeError` says so). Declaring it keyword-only, with the `INJECTED` default, keeps callers from colliding with it.

### Without a `tx` parameter

`require_transaction()` returns the innermost running transaction and raises `RuntimeError` outside one; `current_transaction()` returns `None` instead. It is backed by a `ContextVar`, so it is correct across threads and asyncio tasks, and lets helper functions register undo steps without the transaction being passed through every layer.

```python
from python_wrap.state import current_transaction, require_transaction, transactional

undone: list[str] = []

def create_resource(name: str) -> None:
    tx = require_transaction()
    tx.on_rollback(undone.append, f"delete {name}")

@transactional
def provision() -> None:
    create_resource("bucket")
    create_resource("queue")
    raise RuntimeError

try:
    provision()
except RuntimeError:
    pass

assert undone == ["delete queue", "delete bucket"]
assert current_transaction() is None
```

Nested `@transactional` calls each get their own transaction.
