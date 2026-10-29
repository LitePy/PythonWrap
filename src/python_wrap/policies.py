"""Named, inspectable resilience policies loaded from Python mappings or JSON env vars."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from python_wrap import _fork
from python_wrap.policy import Policy, policy

__all__ = ["PolicyRegistry", "policies", "resilient"]
F = TypeVar("F", bound=Callable[..., Any])


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
        with self._lock:
            for name, settings in staged.items():
                self._settings[name] = settings
                self._compiled[name] = compiled[name]
                self._versions[name] = self._versions.get(name, 0) + 1

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
                    roles[role] = RateLimiter(**options)
            elif role == "bulkhead":
                options.setdefault("name", name)
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


policies = PolicyRegistry()
resilient = policies.resilient
