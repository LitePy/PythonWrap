# Django: cached services, commit hooks and API evolution

## The problem

A Django codebase accumulates a service layer: functions such as `get_plan(user)` or `upgrade(user, plan)` that views, tasks and management commands all call. Three recurring needs:

1. **Cache reads** with an explicit way to invalidate them when the data changes.
2. **Run side effects only after success**: send the email, clear the cache, enqueue the job only when the work really committed. Django's answer for database work is `transaction.on_commit`. What about the non-database parts (a file written, an external API charged) that have to be undone if a later step fails?
3. **Rename and remove functions** without breaking callers all at once.

The decorators involved (`memoize`, `transactional`, `deprecate`) know nothing about Django, so this page starts with a framework-free service layer you can run, then shows where Django plugs in.

## The framework-free service layer

```python
import contextlib
import warnings

from python_wrap import deprecate, memoize
from python_wrap.state import INJECTED, Transaction, transactional

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
def upgrade(user_id: int, plan: str, *, fail: bool = False, tx: Transaction = INJECTED) -> None:
    previous = DB[user_id]
    DB[user_id] = plan
    tx.on_rollback(DB.__setitem__, user_id, previous)  # undo the write if anything fails
    if fail:
        raise RuntimeError("payment declined")
    # Runs only if the whole function succeeds, like transaction.on_commit().
    tx.on_commit(get_plan.cache_delete, user_id)
    tx.on_commit(OUTBOX.append, f"user {user_id} is now on {plan}")


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


main()
```

### How it works

* **`get_plan`** is `memoize(ttl=300)`: repeated reads in a process are free for five minutes, and `get_plan.cache_delete(user_id)` drops exactly one entry.
* **`upgrade`** is `@transactional`. The function body registers what to do in each outcome on the current `Transaction`:
    * `tx.on_rollback(fn, *args)` registers an *undo step*. If the function raises, undo steps run in reverse order and the original exception propagates.
    * `tx.on_commit(fn, *args)` registers a *follow-up step*. If the function returns normally, they run in registration order.
  This is the same idea as Django's `transaction.on_commit`, extended with rollback hooks for things a database cannot roll back for you.
* **`fetch_plan`** is the old name, kept working with `@deprecate`. Every call emits a `DeprecationWarning` that points at the **caller's** line, with the version information in the message.

## Where Django plugs in

With the ORM, keep the database changes inside `transaction.atomic()` and use Django's own `on_commit` for follow-ups that depend on the database commit. Use python-wrap where Django has nothing: caching with explicit keys, undo steps for non-database side effects, and deprecations.

<!-- skip -->
```python
# services.py
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from python_wrap import deprecate, memoize
from python_wrap.state import INJECTED, Transaction, transactional

from .models import Subscription
from .tasks import send_receipt


@memoize(ttl=300, maxsize=10_000)
def get_plan(user_id: int) -> str:
    # Return a plain value, not a model instance: cached objects are shared.
    return Subscription.objects.values_list("plan", flat=True).get(user_id=user_id)


@receiver(post_save, sender=Subscription)
def invalidate_plan(sender, instance, **kwargs) -> None:
    # Runs for every save, including saves from the admin and from other code paths.
    transaction.on_commit(lambda: get_plan.cache_delete(instance.user_id))


@transactional
def upgrade(user_id: int, plan: str, *, tx: Transaction = INJECTED) -> None:
    with transaction.atomic():  # the database part: Django rolls this back by itself
        Subscription.objects.filter(user_id=user_id).update(plan=plan)
        transaction.on_commit(lambda: send_receipt.delay(user_id))  # after the DB commit

    charge_id = billing_api.charge(user_id, plan)  # not a database operation...
    tx.on_rollback(billing_api.refund, charge_id)  # ...so register its undo step
    audit_log.write(user_id, plan)  # if this raises, the charge is refunded


@deprecate(since="2.0", removed_in="3.0", alternative="get_plan")
def fetch_plan(user_id: int) -> str:
    return get_plan(user_id)
```

Run the Django tests with `python -W error::DeprecationWarning manage.py test` (or `filterwarnings = error::DeprecationWarning` in pytest) to find every remaining caller of `fetch_plan`: the warning names the file and line of the call.

## Pitfalls

!!! warning "The cache is per process"
    Each gunicorn worker has its own `get_plan` cache. `cache_delete` in one process does not clear the others, so a plan change is visible everywhere only after `ttl` expires. That is acceptable for data that tolerates a few minutes of staleness. For data that must be consistent across processes, use Django's cache framework (Redis or Memcached) instead, and use `memoize` for pure, cheap-to-recompute functions.

!!! warning "Cache values, not model instances"
    A memoized function returns the same object to every caller. A cached model instance carries a reference to its fields, its related caches and (for lazy relations) the ability to hit the database. Return strings, numbers, tuples or frozen dataclasses.

* **`python-wrap`'s `Transaction` is not a database transaction.** It does not give isolation or atomicity; it only runs callbacks. Keep database writes inside `transaction.atomic()` and use the `Transaction` for the steps that need an explicit undo.
* **Django's `TestCase` does not fire `on_commit` callbacks** (the test transaction never commits); use `self.captureOnCommitCallbacks(execute=True)`. python-wrap's `on_commit` callbacks run when the `@transactional` function returns, so they fire in tests.
* Undo steps must be safe to run and ideally idempotent; a failing undo step is logged and the remaining ones still run.
* Do not put `@memoize` on methods of Django model instances unless you accept that the cache keeps every instance alive; use a module-level function keyed by primary key.
* `deprecate` warns on **every call**. Python's default filters show a `DeprecationWarning` only when triggered in `__main__`, so make sure your test runner shows them.

## Test it

The service layer is plain Python, so its tests do not need the database. Reset state at the start of each test, and assert on the *order* of effects.

```python
import contextlib
import warnings

from python_wrap import deprecate, memoize
from python_wrap.state import INJECTED, Transaction, transactional

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
def upgrade(user_id: int, plan: str, *, fail: bool = False, tx: Transaction = INJECTED) -> None:
    previous = DB[user_id]
    DB[user_id] = plan
    tx.on_rollback(DB.__setitem__, user_id, previous)  # undo the write if anything fails
    if fail:
        raise RuntimeError("payment declined")
    # Runs only if the whole function succeeds, like transaction.on_commit().
    tx.on_commit(get_plan.cache_delete, user_id)
    tx.on_commit(OUTBOX.append, f"user {user_id} is now on {plan}")

import pytest

from python_wrap.testing import spy_on


def reset() -> None:
    DB.update({1: "free", 2: "pro"})
    OUTBOX.clear()
    QUERIES.clear()
    get_plan.cache_clear()


def test_reads_are_cached_and_invalidated_on_commit() -> None:
    reset()
    get_plan(1)
    get_plan(1)
    assert QUERIES == [1]
    upgrade(1, "pro")
    assert get_plan(1) == "pro"
    assert QUERIES == [1, 1]


def test_failed_upgrade_rolls_back_and_sends_nothing() -> None:
    reset()
    with pytest.raises(RuntimeError, match="declined"):
        upgrade(1, "pro", fail=True)
    assert DB[1] == "free"
    assert OUTBOX == []


def test_commit_hooks_run_in_order() -> None:
    reset()
    with spy_on(get_plan, "cache_delete") as invalidate:
        upgrade(1, "pro")
    assert invalidate.call_count == 1
    assert OUTBOX == ["user 1 is now on pro"]


def test_deprecated_name_still_works_and_warns() -> None:
    reset()
    with pytest.warns(DeprecationWarning, match="use get_plan instead"):
        assert fetch_plan(2) == "pro"


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

!!! note "`spy_on` and cached functions"
    `spy_on(get_plan, "cache_delete")` temporarily replaces an attribute of the memoized function and restores it afterwards, so you can check that the invalidation hook was called without touching production code.

For Django's own test runner, add a `setUp` that calls `get_plan.cache_clear()`, or wrap the test in `python_wrap.testing.disable_caching()` to bypass every cache for that test.
