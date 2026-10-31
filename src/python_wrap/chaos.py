"""Fault injection for resilience code: latency, errors and canned results on demand.

Mark the calls you want to be able to break with `fault_point`, then switch faults on
from a test (``chaos.inject(...)``) or, for a game day in staging, from an environment
variable (``chaos.enable_from_env()``). With no rule active a fault point costs one
truthiness check on a tuple, and nothing is ever injected unless a rule was added, so
leaving the markers in production code is safe.

Latency waits through ``_core.sleep`` / ``_core.async_sleep``: with
``python_wrap.testing.fake_clock`` it advances a fake clock; otherwise it sleeps for
real time.
"""

from __future__ import annotations

import builtins
import fnmatch
import functools
import importlib
import json
import math
import os
import random
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar

from python_wrap import _core, _fork

__all__ = ["Fault", "FaultInjector", "chaos", "fault_point"]

F = TypeVar("F", bound=Callable[..., Any])


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


_UNSET = _Unset()

ExceptionSpec = BaseException | type[BaseException] | Callable[[], BaseException]


def _check_latency(latency: Any) -> float | tuple[float, float] | None:
    if latency is None:
        return None
    if isinstance(latency, (tuple, list)):
        if len(latency) != 2:
            raise ValueError("latency must be seconds or a (min, max) pair")
        low, high = latency
        for v in (low, high):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError("latency bounds must be finite numbers")
        if low < 0 or high < low:
            raise ValueError("latency (min, max) needs 0 <= min <= max")
        return (float(low), float(high))
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError("latency must be seconds or a (min, max) pair")
    if not math.isfinite(latency) or latency < 0:
        raise ValueError("latency must be a finite number >= 0")
    return float(latency)


class Fault:
    """Handle for one injection rule. Also a context manager that removes the rule.

    Attributes:
        name: The point name or fnmatch-style pattern the rule applies to.
        injected: How many times this rule has fired.
    """

    def __init__(
        self,
        injector: FaultInjector,
        name: str,
        latency: float | tuple[float, float] | None,
        exception: ExceptionSpec | None,
        rate: float,
        times: int | None,
        result: Any,
    ) -> None:
        self._injector = injector
        self.name = name
        self.latency = latency
        self.exception = exception
        self.rate = rate
        self.times = times
        self.result = result
        self.injected = 0

    @property
    def active(self) -> bool:
        """True while the rule is installed and has not used up ``times``."""
        return self in self._injector._rules and (self.times is None or self.injected < self.times)

    def remove(self) -> None:
        """Remove the rule. Removing twice is harmless."""
        self._injector._remove(self)

    def __enter__(self) -> Fault:
        return self

    def __exit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.remove()

    def __repr__(self) -> str:
        parts = [repr(self.name)]
        if self.latency is not None:
            parts.append(f"latency={self.latency}")
        if self.exception is not None:
            parts.append(f"exception={self.exception!r}")
        if self.result is not _UNSET:
            parts.append(f"result={self.result!r}")
        parts.append(f"rate={self.rate}")
        if self.times is not None:
            parts.append(f"times={self.times}")
        return f"Fault({', '.join(parts)})"

    def __reduce__(self) -> Any:
        _core.refuse_pickle(self, "Fault rules belong to one process.")


@dataclass(frozen=True)
class _Action:
    kind: str
    pattern: str
    seconds: float = 0.0
    exception: BaseException | None = None
    result: Any = None


def _make_exception(spec: ExceptionSpec) -> BaseException:
    if isinstance(spec, BaseException):
        return spec
    if isinstance(spec, type):
        exc: BaseException = spec()
        return exc
    made: Any = spec()
    if not isinstance(made, BaseException):
        raise TypeError(f"exception factory returned {made!r}, not an exception")
    return made


class FaultInjector:
    """Holds the active rules and decides, per call, what to inject.

    Use the process-wide instance `chaos`; create your own for isolated tests.

    Args:
        seed: Seed for the random source (rate decisions and latency ranges).
        rng: An existing `random.Random` to use instead of ``seed``.

    Attributes:
        enabled: Global kill switch. While false, nothing is injected and rules are kept.
    """

    def __init__(self, seed: int | None = None, rng: random.Random | None = None) -> None:
        self._lock = _fork.ForkSafeLock()
        self._rules: tuple[Fault, ...] = ()  # copy-on-write: readers need no lock
        self._counts: dict[str, int] = {}
        self._rng = rng if rng is not None else random.Random(seed)
        self.enabled = True

    def reseed(self, seed: int | None) -> None:
        """Restart the random source from ``seed`` for reproducible runs."""
        with self._lock:
            self._rng = random.Random(seed)

    def inject(
        self,
        name: str,
        *,
        latency: float | tuple[float, float] | None = None,
        exception: ExceptionSpec | None = None,
        rate: float = 1.0,
        times: int | None = None,
        result: Any = _UNSET,
    ) -> Fault:
        """Add a rule for the fault point(s) matching ``name``.

        Args:
            name: A point name or fnmatch pattern such as ``"payments.*"``.
            latency: Seconds to wait before the call, or a ``(min, max)`` range.
            exception: Raise this instead of calling: an instance, an exception class,
                or a zero-argument factory returning an exception.
            rate: Probability in ``[0, 1]`` that a matching call is affected.
            times: Inject at most this many times, then the rule goes quiet.
            result: Return this value instead of calling the function.

        Returns:
            A `Fault` handle; use it as a context manager or call ``remove()``.

        Raises:
            ValueError: For an empty name, nothing to inject, ``exception`` together with
                ``result``, or out-of-range ``rate``/``times``/``latency``.

        Example:
            ```python
            from python_wrap.chaos import chaos, fault_point

            @fault_point("inventory.lookup")
            def lookup() -> str:
                return "real"

            with chaos.inject("inventory.*", result="canned"):
                assert lookup() == "canned"
            assert lookup() == "real"
            ```
        """
        if not isinstance(name, str) or not name:
            raise ValueError("fault name must be a non-empty string")
        lat = _check_latency(latency)
        if lat is None and exception is None and result is _UNSET:
            raise ValueError("inject() needs at least one of latency, exception, result")
        if exception is not None and result is not _UNSET:
            raise ValueError("exception and result are mutually exclusive")
        if exception is not None and not (
            isinstance(exception, BaseException)
            or (isinstance(exception, type) and issubclass(exception, BaseException))
            or callable(exception)
        ):
            raise ValueError("exception must be an instance, a class or a zero-argument factory")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError("rate must be a number between 0 and 1")
        if times is not None and (
            isinstance(times, bool) or not isinstance(times, int) or times < 1
        ):
            raise ValueError("times must be None or an integer >= 1")
        fault = Fault(self, name, lat, exception, float(rate), times, result)
        with self._lock:
            self._rules = (*self._rules, fault)
        return fault

    def _remove(self, fault: Fault) -> None:
        with self._lock:
            self._rules = tuple(r for r in self._rules if r is not fault)

    def clear(self) -> None:
        """Remove every rule and reset the injection counts."""
        with self._lock:
            self._rules = ()
            self._counts = {}

    def active(self) -> list[Fault]:
        """The installed rules that can still fire, in the order they were added."""
        return [f for f in self._rules if f.active]

    def stats(self, name: str | None = None) -> int | dict[str, int]:
        """Injection counts per fault point.

        Args:
            name: A point name. Omit for a ``{point: count}`` mapping of all points.

        Returns:
            The number of injections at ``name`` (0 if none), or the whole mapping.
        """
        with self._lock:
            if name is None:
                return dict(self._counts)
            return self._counts.get(name, 0)

    @contextmanager
    def open_circuit(self, breaker: Any) -> Iterator[Any]:
        """Force a `CircuitBreaker` open for the block, then put it back.

        On exit a breaker that was closed (or half-open) is reset to closed; one that
        was already open is left open.

        Example:
            ```python
            from python_wrap import CircuitBreaker
            from python_wrap.chaos import chaos

            breaker = CircuitBreaker(name="db")
            with chaos.open_circuit(breaker):
                assert breaker.state.value == "open"
            assert breaker.state.value == "closed"
            ```
        """
        was_open = breaker.state.value == "open"
        breaker.force_open()
        try:
            yield breaker
        finally:
            if not was_open:
                breaker.reset()

    def enable_from_env(
        self, var: str = "PW_CHAOS", environ: Mapping[str, str] | None = None
    ) -> list[Fault]:
        """Install the rules described by a JSON list in an environment variable.

        Does nothing (returns ``[]``) when the variable is unset or blank, so it is safe
        to call at startup in every environment. Each rule is an object with ``name``
        and any of ``latency`` (seconds or ``[min, max]``), ``exception`` (a builtin
        name such as ``"ConnectionError"`` or ``"module:Class"``), ``result`` (a JSON
        value), ``rate`` and ``times``. Rules are validated first; a bad one installs
        none.

        Example value::

            [{"name": "payments.*", "latency": 0.5, "rate": 0.1},
             {"name": "inventory", "exception": "ConnectionError"}]

        Raises:
            ValueError: On invalid JSON, a non-list, unknown keys, an unknown exception
                or an invalid rule.
        """
        raw = (environ if environ is not None else os.environ).get(var, "")
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{var} is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"{var} must be a JSON list of rule objects")
        allowed = {"name", "latency", "exception", "rate", "times", "result"}
        parsed: list[dict[str, Any]] = []
        for i, item in enumerate(data):
            where = f"{var} rule #{i}"
            if not isinstance(item, dict):
                raise ValueError(f"{where} must be an object")
            unknown = set(item) - allowed
            if unknown:
                raise ValueError(f"{where} has unknown key(s): {', '.join(sorted(unknown))}")
            if "name" not in item:
                raise ValueError(f"{where} is missing 'name'")
            kwargs = {k: v for k, v in item.items() if k != "name"}
            if "exception" in kwargs:
                kwargs["exception"] = _resolve_exception(kwargs["exception"], where)
            parsed.append({"name": item["name"], **kwargs})
        # Validate everything by building throwaway handles before touching our rules.
        probe = FaultInjector()
        try:
            for kwargs in parsed:
                probe.inject(**kwargs)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{var}: {exc}") from exc
        return [self.inject(**kwargs) for kwargs in parsed]

    def point(self, name: str) -> Callable[[F], F]:
        """Alias of `fault_point` bound to this injector (for policy ``inner=``)."""
        return fault_point(name, injector=self)

    # -- internals -----------------------------------------------------------------

    def _plan(self, point: str) -> list[_Action]:
        actions: list[_Action] = []
        with self._lock:
            for rule in self._rules:
                if rule.times is not None and rule.injected >= rule.times:
                    continue
                if not fnmatch.fnmatchcase(point, rule.name):
                    continue
                if rule.rate < 1.0 and self._rng.random() >= rule.rate:
                    continue
                rule.injected += 1
                self._counts[point] = self._counts.get(point, 0) + 1
                if rule.latency is not None:
                    lat = rule.latency
                    secs = lat if isinstance(lat, float) else self._rng.uniform(lat[0], lat[1])
                    actions.append(_Action("latency", rule.name, seconds=secs))
                if rule.exception is not None:
                    actions.append(
                        _Action("exception", rule.name, exception=_make_exception(rule.exception))
                    )
                    break
                if rule.result is not _UNSET:
                    actions.append(_Action("result", rule.name, result=rule.result))
                    break
        return actions


def _resolve_exception(spec: Any, where: str) -> type[BaseException]:
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"{where}: 'exception' must be a string")
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
        try:
            obj = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"{where}: cannot import exception {spec!r}: {exc}") from exc
    else:
        obj = getattr(builtins, spec, None)
    if not (isinstance(obj, type) and issubclass(obj, BaseException)):
        raise ValueError(
            f"{where}: unknown exception {spec!r} (use a builtin name or 'module:Class')"
        )
    return obj


chaos = FaultInjector()
"""The process-wide injector used by `fault_point` unless given another."""


def _emit(point: str, action: _Action) -> None:
    _core.emit("chaos.injected", point, kind=action.kind, point=point, pattern=action.pattern)


def fault_point(name: str, *, injector: FaultInjector | None = None) -> Callable[[F], F]:
    """Decorator marking a call that chaos rules may slow down, break or replace.

    Works on sync and async functions and keeps the signature. With no active rule it
    adds a single check. Because it is a plain decorator it can sit under other
    layers, e.g. ``policy(inner=fault_point("x"))``, so retries and timeouts see the
    injected faults.

    Args:
        name: The point name that rules match (exact or fnmatch pattern).
        injector: Use this injector instead of the global `chaos`.

    Example:
        ```python
        from python_wrap import retry
        from python_wrap.chaos import chaos, fault_point

        @retry(max_attempts=3, delay=0, on=ConnectionError)
        @fault_point("db.query")
        def query() -> str:
            return "rows"

        with chaos.inject("db.query", exception=ConnectionError, times=2):
            assert query() == "rows"
        ```
    """
    if not isinstance(name, str) or not name:
        raise ValueError("fault_point name must be a non-empty string")

    def decorate(func: F) -> F:
        if _core.is_async(func):

            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                inj = injector or chaos
                if inj._rules and inj.enabled:
                    for action in inj._plan(name):
                        _emit(name, action)
                        if action.kind == "latency":
                            await _core.async_sleep(action.seconds)
                        elif action.kind == "exception":
                            assert action.exception is not None
                            raise action.exception
                        else:
                            return action.result
                return await func(*args, **kwargs)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            inj = injector or chaos
            if inj._rules and inj.enabled:
                for action in inj._plan(name):
                    _emit(name, action)
                    if action.kind == "latency":
                        _core.sleep(action.seconds)
                    elif action.kind == "exception":
                        assert action.exception is not None
                        raise action.exception
                    else:
                        return action.result
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
