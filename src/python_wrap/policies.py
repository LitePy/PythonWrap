"""Named, inspectable resilience policies loaded from mappings, JSON env vars or sources.

Mappings may refer to objects by name: ``"on": ["builtins.ConnectionError"]``,
``"retry_if": "db_transient"`` and ``"backend": "@default"``. Names resolve only
through explicit registries (`PolicyRegistry.register_exception`,
`register_predicate`, `register_backend`); builtin exception names and the predicates
in `python_wrap.predicates` are registered already. No dotted path is ever imported.
"""

from __future__ import annotations

import builtins
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap.policy import Policy, policy

__all__ = [
    "EnvSource",
    "FileSource",
    "PolicyRegistry",
    "PolicySource",
    "RedisSource",
    "Watch",
    "policies",
    "resilient",
]
F = TypeVar("F", bound=Callable[..., Any])

_EXCEPTION_KEYS = frozenset({"on", "exclude"})
_PREDICATE_KEYS = frozenset({"retry_if", "retry_if_result"})


class PolicySource(Protocol):
    """Where `PolicyRegistry.watch` reads a whole mapping of named policies from."""

    def read(self) -> Mapping[str, Mapping[str, Any]]: ...


class EnvSource:
    """``PW_POLICY_<NAME>={JSON}`` environment variables (re-read on every poll)."""

    def __init__(self, prefix: str = "PW_POLICY_") -> None:
        self.prefix = prefix

    def read(self) -> dict[str, Any]:
        return {
            key[len(self.prefix) :]: json.loads(value)
            for key, value in os.environ.items()
            if key.startswith(self.prefix)
        }


class FileSource:
    """A JSON file holding ``{"name": {settings}, ...}``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def read(self) -> Any:
        return json.loads(self.path.read_text(encoding="utf-8"))


class RedisSource:
    """One Redis string key holding the JSON mapping, shared by every replica."""

    def __init__(self, client: Any, key: str = "python-wrap:policies") -> None:
        self.client, self.key = client, key

    def read(self) -> Any:
        raw = self.client.get(self.key)
        if raw is None:
            return {}
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


class Watch:
    """A running `PolicyRegistry.watch`; ``stop()`` ends it. Does not survive a fork."""

    def __init__(self, registry: PolicyRegistry, source: PolicySource, interval: float):
        self.registry, self.source, self.interval = registry, source, interval
        self._stop = threading.Event()
        self._last: str | None = None
        self.poll()  # the first read happens now, so a broken source fails loudly
        self._thread = threading.Thread(target=self._run, daemon=True, name="python-wrap-policies")
        self._thread.start()

    def poll(self) -> bool:
        """Read the source once; apply it if it changed. True when a change was applied."""
        try:
            mapping = self.source.read()
            text = json.dumps(mapping, sort_keys=True, default=repr)
            if text == self._last:
                return False
            self.registry.load_from_mapping(mapping)
        except Exception as exc:
            # A bad mapping keeps the current policies; the source is retried next poll.
            _core.emit("config.rejected", "policies", error=type(exc).__name__)
            return False
        self._last = text
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)


class PolicyRegistry:
    """A registry whose decorator resolves the named policy at each call.

    Configuration updates are atomic: each in-flight call retains its resolved stack.
    Each registered configuration compiles one shared policy, so named breaker,
    bulkhead and limiter state is shared across its decorated functions. A replacement
    starts a new policy generation.
    """

    def __init__(self) -> None:
        self._lock = _fork.ForkSafeRLock()
        self._settings: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._compiled: dict[str, Policy] = {}
        self._exceptions: dict[str, type[BaseException]] = {
            f"builtins.{name}": value
            for name, value in vars(builtins).items()
            if isinstance(value, type) and issubclass(value, BaseException)
        }
        from python_wrap import predicates

        self._predicates: dict[str, Callable[..., Any]] = {
            name: getattr(predicates, name)
            for name in predicates.__all__
            if name.endswith("_transient")
        }
        self._backends: dict[str, Any] = {}

    # -- names a mapping may refer to ------------------------------------------------
    def register_exception(self, name: str, exception: type[BaseException]) -> None:
        """Allow ``name`` (e.g. ``"requests.exceptions.Timeout"``) in ``on``/``exclude``."""
        if not (isinstance(exception, type) and issubclass(exception, BaseException)):
            raise TypeError("register_exception needs an exception class")
        self._exceptions[name] = exception

    def register_predicate(self, name: str, predicate: Callable[..., Any]) -> None:
        """Allow ``name`` as a ``retry_if``/``retry_if_result`` value."""
        if not callable(predicate):
            raise TypeError("register_predicate needs a callable")
        self._predicates[name] = predicate

    def register_backend(self, name: str, backend: Any) -> None:
        """Allow ``"@name"`` wherever a mapping needs a backend object."""
        self._backends[name] = backend

    def _resolve(self, key: str | None, value: Any) -> Any:
        """Turn names in a JSON-shaped mapping into the registered objects."""
        if isinstance(value, Mapping):
            return {k: self._resolve(k, v) for k, v in value.items()}
        if key in _EXCEPTION_KEYS and isinstance(value, (str, list, tuple)):
            names = [value] if isinstance(value, str) else list(value)
            found = []
            for name in names:
                if not isinstance(name, str):
                    found.append(name)
                elif name in self._exceptions:
                    found.append(self._exceptions[name])
                else:
                    raise ValueError(f"unknown exception name {name!r}; register_exception it")
            return tuple(found)
        if key in _PREDICATE_KEYS and isinstance(value, str):
            if value not in self._predicates:
                raise ValueError(f"unknown predicate {value!r}; register_predicate it")
            return self._predicates[value]
        if isinstance(value, str) and value.startswith("@"):
            if value[1:] not in self._backends:
                raise ValueError(f"unknown backend {value!r}; register_backend it")
            return self._backends[value[1:]]
        if isinstance(value, list):
            return [self._resolve(None, item) for item in value]
        return value

    def watch(self, source: PolicySource, interval: float = 30) -> Watch:
        """Poll ``source`` every ``interval`` seconds and apply each changed mapping.

        A whole mapping is validated before it is applied; a bad one keeps the current
        policies and emits ``config.rejected``. Each applied policy emits
        ``config.changed``. Runtime state carries over when a policy keeps its name: an
        open in-process breaker stays open under the new thresholds (shared breakers
        keep their state in the backend anyway).
        """
        if interval <= 0:
            raise ValueError("interval must be positive")
        return Watch(self, source, interval)

    def register(self, name: str, **settings: Any) -> None:
        """Atomically replace one named policy after validating its full configuration."""
        self.load_from_mapping({name: settings})

    def load_from_mapping(self, mapping: Mapping[str, Mapping[str, Any]]) -> None:
        """Validate an entire mapping before atomically applying it.

        Configuration containers are copied without copying live clients or callbacks.
        Named breaker/limiter/bulkhead instances are shared across decorated functions.
        """

        def snapshot(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: snapshot(item) for key, item in value.items()}
            if isinstance(value, list):
                return [snapshot(item) for item in value]
            if isinstance(value, tuple):
                return tuple(snapshot(item) for item in value)
            return value

        staged: dict[str, dict[str, Any]] = {}
        compiled: dict[str, Policy] = {}
        for name, settings in mapping.items():
            if not isinstance(name, str) or not name or not isinstance(settings, Mapping):
                raise ValueError("policies need nonempty names and mapping configurations")
            staged[name] = snapshot(settings)
            compiled[name] = self._compile(name, staged[name])
        changed = []
        with self._lock:
            for name, settings in staged.items():
                if self._settings.get(name) == settings and name in self._compiled:
                    continue  # unchanged: keep the generation and its live state
                old = self._compiled.get(name)
                if old is not None:
                    _carry_state(old, compiled[name])
                self._settings[name] = settings
                self._compiled[name] = compiled[name]
                self._versions[name] = self._versions.get(name, 0) + 1
                changed.append((name, self._versions[name]))
            if changed and self is policies and not _health_registered:
                _register_health()
        for name, generation in changed:
            _core.emit("config.changed", name, generation=generation)

    def load_from_env(self, prefix: str = "PW_POLICY_") -> None:
        """Read NAME={JSON policy settings} from env; existing names are case-sensitive."""
        mapping = {
            key[len(prefix) :]: json.loads(value)
            for key, value in os.environ.items()
            if key.startswith(prefix)
        }
        self.load_from_mapping(mapping)

    def describe(self, name: str | None = None) -> dict[str, Any]:
        """Return effective layer names and settings; do not include object reprs/secrets."""
        with self._lock:
            selected = self._settings if name is None else {name: self._settings[name]}
            return {
                key: {
                    "order": self._compiled[key].order,
                    "roles": tuple(value),
                    "generation": self._versions[key],
                }
                for key, value in selected.items()
            }

    def _compile(self, name: str, settings: Mapping[str, Any]) -> Policy:
        from python_wrap.bulkhead import Bulkhead
        from python_wrap.caching import memoize
        from python_wrap.circuit import CircuitBreaker
        from python_wrap.concurrency import RateLimiter, timeout
        from python_wrap.quotas import shared_rate_limit
        from python_wrap.retry import fallback, retry
        from python_wrap.shared import resolve_shared

        sb = resolve_shared(None)
        settings = self._resolve(None, settings)
        allowed = {"timeout", "retry", "breaker", "limiter", "bulkhead", "fallback", "cache"}
        unknown = set(settings) - allowed
        if unknown:
            raise ValueError(f"unknown policy roles: {sorted(unknown)}")
        roles: dict[str, Any] = {}
        for role, value in settings.items():
            if value is None:
                continue
            options = dict(value) if isinstance(value, Mapping) else value
            if role == "timeout":
                roles[role] = timeout(**options) if isinstance(options, dict) else timeout(options)
            elif role == "retry":
                roles[role] = retry(**options)
            elif role == "breaker":
                options.setdefault("name", name)
                if sb is not None and "backend" not in options and sb.breaker() is not None:
                    options["backend"] = sb.breaker()
                roles[role] = CircuitBreaker(**options)
            elif role == "limiter":
                if "backend" in options:
                    backend = options.pop("backend")
                    roles[role] = shared_rate_limit(backend, **options)
                else:
                    if "rate" in options:
                        options["calls"] = options.pop("rate")
                    if "per" in options:
                        options["period"] = options.pop("per")
                    window = (
                        None
                        if sb is None or "calls" not in options
                        else sb.rate_window(
                            options.get("name", name), options["calls"], options.get("period", 1.0)
                        )
                    )
                    roles[role] = RateLimiter(**options, backend=window)
            elif role == "bulkhead":
                options.setdefault("name", name)
                if sb is not None and "backend" not in options:
                    options["backend"] = sb.lease_backend()
                roles[role] = Bulkhead(**options)
            elif role == "cache":
                roles[role] = memoize(**options)
            else:
                roles[role] = (
                    fallback(**options) if isinstance(options, dict) else fallback(options)
                )
        return policy(name=name, **roles)

    def resilient(self, name: str) -> Callable[[F], F]:
        """Decorate with a named policy, resolving configuration at call time."""
        import functools

        from python_wrap import _core

        def decorate(func: F) -> F:
            version = -1
            compiled: Any = None
            lock = _fork.ForkSafeLock()

            def resolve() -> Any:
                nonlocal version, compiled
                with self._lock:
                    if name not in self._settings:
                        raise KeyError(f"policy {name!r} is not registered")
                    current = self._versions[name]
                    selected = self._compiled[name]
                with lock:
                    if current != version:
                        compiled = selected(func)
                        version = current
                    return compiled

            if _core.is_async(func):

                @functools.wraps(func)
                async def aw(*args: Any, **kw: Any) -> Any:
                    return await resolve()(*args, **kw)

                return cast(F, aw)

            @functools.wraps(func)
            def sw(*args: Any, **kw: Any) -> Any:
                return resolve()(*args, **kw)

            return cast(F, sw)

        return decorate


def _carry_state(old: Policy, new: Policy) -> None:
    """Keep an in-process breaker's state across a reload of the same policy name."""
    from python_wrap.circuit import CircuitBreaker

    before = old._roles.get("breaker", ())
    after = new._roles.get("breaker", ())
    if (
        before
        and after
        and isinstance(before[0], CircuitBreaker)
        and isinstance(after[0], CircuitBreaker)
    ):
        after[0]._adopt(before[0])


policies = PolicyRegistry()
resilient = policies.resilient
_health_registered = False


def _register_health() -> None:
    """Report each named policy's generation in `python_wrap.health.health.report()`."""
    global _health_registered
    from python_wrap.health import health

    _health_registered = True
    health.register_check(
        "policies", lambda: {"healthy": True, "generations": dict(policies._versions)}
    )
