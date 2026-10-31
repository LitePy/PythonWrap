"""Drop-in helpers for code that uses a hand-rolled Redis lock helper.

Many codebases carry a small module shaped like this::

    with named_lock(name, suffix): ...
    if acquire_lock(name): ...
    release_lock(name)

This module offers the same call shapes on top of python-wrap's owner-checked,
auto-renewing leases, so migrating is usually a one-line import change plus a single
`configure_locks` call at start-up.

Improvement over a plain ``SET NX`` / ``DEL`` helper: every acquisition has a unique
token, and release only removes the lock if that token still owns it. A helper that
issues a bare ``DEL`` can release a lock that expired and was taken over by another
worker; here that release is a no-op that returns ``False``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from typing import Any, Literal, overload

from python_wrap import _core, _fork
from python_wrap._runtime import seconds
from python_wrap.coordination import _LOCAL, AlreadyRunning, Lease

__all__ = [
    "RunLocks",
    "acquire_lock",
    "configure_locks",
    "named_lock",
    "release_lock",
]

_default_backend: Any = None


def configure_locks(backend: Any) -> None:
    """Set the default lease backend used when none is passed explicitly.

    Args:
        backend: A lease backend such as `RedisLock`, or ``None`` to go back to the
            in-process default.

    Example:
        ```python
        from python_wrap import LocalLease
        from python_wrap.compat import configure_locks, named_lock

        configure_locks(LocalLease())
        with named_lock("nightly-import", "tenant-7") as lease:
            assert lease.key == "lock:nightly-import:tenant-7"
        configure_locks(None)
        ```
    """
    global _default_backend
    _default_backend = backend


def _resolve(backend: Any) -> Any:
    if backend is not None:
        return backend
    return _default_backend if _default_backend is not None else _LOCAL


def _key(prefix: str, name: str, suffix: object | None = None) -> str:
    if not name:
        raise ValueError("lock name must not be empty")
    key = prefix + name
    return key if suffix is None else f"{key}:{suffix}"


@contextlib.contextmanager
def _named_lock(
    key: str, backend: Any, ttl: float, wait: bool, timeout: float | None, skip: bool
) -> Iterator[Lease | Literal[False]]:
    lease = Lease(key, backend=_resolve(backend), ttl=ttl, wait=wait, timeout=timeout)
    try:
        lease.__enter__()
    except AlreadyRunning:
        if not skip:
            raise
        yield False
        return
    try:
        yield lease
    except BaseException as exc:
        lease.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        lease.__exit__(None, None, None)


@overload
def named_lock(
    name: str,
    suffix: object | None = ...,
    *,
    backend: Any = ...,
    ttl: float | str = ...,
    wait: bool = ...,
    timeout: float | None = ...,
    prefix: str = ...,
    skip: Literal[False] = ...,
) -> contextlib.AbstractContextManager[Lease]: ...


@overload
def named_lock(
    name: str,
    suffix: object | None = ...,
    *,
    backend: Any = ...,
    ttl: float | str = ...,
    wait: bool = ...,
    timeout: float | None = ...,
    prefix: str = ...,
    skip: bool,
) -> contextlib.AbstractContextManager[Lease | Literal[False]]: ...


def named_lock(
    name: str,
    suffix: object | None = None,
    *,
    backend: Any = None,
    ttl: float | str = 600,
    wait: bool = False,
    timeout: float | None = None,
    prefix: str = "lock:",
    skip: bool = False,
) -> contextlib.AbstractContextManager[Lease | Literal[False]]:
    """Hold an auto-renewing lease named ``prefix + name [+ ":" + suffix]``.

    Args:
        name: Lock name.
        suffix: Optional discriminator appended after a colon (a tenant id, say).
        backend: Lease backend; defaults to the one set by `configure_locks`, else an
            in-process backend.
        ttl: Lease lifetime in seconds; the lease is renewed while the block runs.
        wait: Wait for the lock instead of failing at once.
        timeout: Longest wait in seconds when ``wait=True``.
        prefix: Key prefix.
        skip: Yield ``False`` instead of raising when the lock is held elsewhere.

    Returns:
        A context manager yielding the active `Lease` (or ``False`` when ``skip`` and
        the lock is held).

    Raises:
        AlreadyRunning: On entering the block, if the lock is held and ``skip`` is false.
        LeaseLost: On leaving the block, if the lease was lost while it ran.

    Example:
        ```python
        from python_wrap.compat import named_lock

        def import_tenant(tenant: str) -> str:
            with named_lock("import", tenant, skip=True) as acquired:
                if not acquired:
                    return "skipped"
                return "imported"

        assert import_tenant("t1") == "imported"
        ```
    """
    return _named_lock(_key(prefix, name, suffix), backend, seconds(ttl), wait, timeout, skip)


class RunLocks:
    """``acquire(name) -> bool`` / ``release(name) -> bool`` locks with owner checks.

    The process remembers the token of each lock it acquired, so `release` only
    removes a lock this process still owns. Locks are not auto-renewed: like a
    ``SET NX EX`` helper they expire after ``ttl`` seconds unless you call `renew`.
    Tokens belong to the process that created them and are forgotten in a forked child.

    Args:
        backend: Lease backend; ``None`` follows `configure_locks` at call time.
        ttl: Lock lifetime in seconds.
        prefix: Key prefix.

    Example:
        ```python
        from python_wrap import LocalLease
        from python_wrap.compat import RunLocks

        locks = RunLocks(LocalLease(), ttl=30)
        assert locks.acquire("report")
        assert not locks.acquire("report")
        assert locks.held("report")
        assert locks.release("report")
        assert not locks.release("report")
        ```
    """

    def __init__(self, backend: Any = None, ttl: float | str = 600, prefix: str = "lock:") -> None:
        self.backend = backend
        self.ttl = seconds(ttl)
        self.prefix = prefix
        self._lock = _fork.ForkSafeLock()
        self._tokens: dict[str, tuple[str, float]] = {}
        self._pid = os.getpid()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._tokens.clear()

    def acquire(self, name: str) -> bool:
        """Try to take the lock. Returns ``False`` if anyone (including us) holds it."""
        key, token = _key(self.prefix, name), uuid.uuid4().hex
        if not _resolve(self.backend).acquire(key, token, self.ttl):
            return False
        with self._lock:
            self._tokens[name] = (token, _core.monotonic() + self.ttl)
        return True

    def release(self, name: str) -> bool:
        """Release a lock this process acquired.

        Returns:
            ``True`` if the lock was still ours and is now free. ``False`` if we never
            held it, or it expired (and may belong to someone else now): it is left alone.
        """
        with self._lock:
            entry = self._tokens.pop(name, None)
        if entry is None:
            return False
        return bool(_resolve(self.backend).release(_key(self.prefix, name), entry[0]))

    def renew(self, name: str) -> bool:
        """Extend a lock we hold by another ``ttl``. ``False`` if it is no longer ours."""
        with self._lock:
            entry = self._tokens.get(name)
        if entry is None:
            return False
        if not _resolve(self.backend).renew(_key(self.prefix, name), entry[0], self.ttl):
            return False
        with self._lock:
            self._tokens[name] = (entry[0], _core.monotonic() + self.ttl)
        return True

    def held(self, name: str) -> bool:
        """True if this process acquired the lock and its ttl has not yet elapsed.

        This is a local estimate with no backend call and no side effects.
        """
        with self._lock:
            entry = self._tokens.get(name)
        return entry is not None and _core.monotonic() < entry[1]


_runlocks = RunLocks()


def acquire_lock(name: str) -> bool:
    """Module-level `RunLocks.acquire` using the `configure_locks` backend."""
    return _runlocks.acquire(name)


def release_lock(name: str) -> bool:
    """Module-level `RunLocks.release` using the `configure_locks` backend."""
    return _runlocks.release(name)
