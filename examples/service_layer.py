"""A service layer with cached reads, commit hooks and a deprecation path.

This is the framework-free shape of what a Django (or any ORM) service module needs:
cached read functions with explicit invalidation, side effects that must only happen
after the work succeeded (the job of ``transaction.on_commit``) and a renamed function
kept alive with a warning. ``Transaction`` here is python-wrap's in-process transaction:
it runs ``on_commit`` callbacks on success and ``on_rollback`` callbacks on failure. It is
not a database transaction; with Django you would still wrap the writes in
``transaction.atomic()`` (see docs/cookbook/django.md).

Run: python examples/service_layer.py
"""

from __future__ import annotations

import contextlib
import warnings

from python_wrap import current_transaction, deprecate, memoize, transactional

DB: dict[int, str] = {1: "free", 2: "pro"}
OUTBOX: list[str] = []  # emails "sent"
QUERIES: list[int] = []


# Cached read. cache_delete(user_id) is the explicit invalidation hook.
@memoize(ttl=300, maxsize=10_000)
def get_plan(user_id: int) -> str:
    QUERIES.append(user_id)
    return DB[user_id]


@deprecate(since="2.0", removed_in="3.0", alternative="get_plan")
def fetch_plan(user_id: int) -> str:
    return get_plan(user_id)


@transactional
def upgrade(user_id: int, plan: str, *, fail: bool = False) -> None:
    tx = current_transaction()  # typed alternative to a `tx` parameter
    assert tx is not None
    previous = DB[user_id]
    DB[user_id] = plan
    tx.on_rollback(DB.__setitem__, user_id, previous)  # undo the write if anything fails
    if fail:
        raise RuntimeError("payment declined")
    # Runs only if the whole function succeeds, like transaction.on_commit().
    tx.on_commit(get_plan.cache_delete, user_id)
    tx.on_commit(OUTBOX.append, f"user {user_id} is now on {plan}")


# --- demo ---


def main() -> None:
    assert get_plan(1) == "free"
    assert get_plan(1) == "free"
    assert QUERIES == [1]  # the second read came from the cache

    with contextlib.suppress(RuntimeError):
        upgrade(1, "pro", fail=True)
    assert DB[1] == "free" and OUTBOX == []  # rolled back, nobody was emailed
    assert get_plan(1) == "free"
    print("failed upgrade: database restored, no email, cache untouched")

    upgrade(1, "pro")
    assert OUTBOX == ["user 1 is now on pro"]
    assert get_plan(1) == "pro"  # the commit hook invalidated the stale entry
    assert QUERIES == [1, 1]
    print("successful upgrade:", OUTBOX[0])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert fetch_plan(2) == "pro"
    assert caught[0].category is DeprecationWarning
    print("deprecation:", caught[0].message)


if __name__ == "__main__":
    main()
