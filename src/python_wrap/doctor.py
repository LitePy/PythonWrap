"""Explain and lint resilience stacks: which layers wrap a function, and what is risky.

`explain` describes a `Policy` (or anything `doctor` accepts); `doctor` walks the layers
of an already decorated function through ``__wrapped__``. Both return a `Report` whose
`findings` flag the combinations that usually cause production incidents: retries
without a per-attempt timeout, retries of non-idempotent operations, unbounded retries,
timeouts longer than the whole retry budget, and layers stacked out of the canonical
order (`python_wrap.policy.ROLES`).

Nothing is called: only configuration attributes are read.

Command line::

    python -m python_wrap doctor package.module:function [--non-idempotent]
"""

from __future__ import annotations

import argparse
import importlib
import itertools
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from python_wrap.policy import ROLES, Policy

__all__ = ["Finding", "Layer", "Report", "doctor", "explain", "main"]

_LAYER_ATTR = "__python_wrap_layer__"


@dataclass(frozen=True)
class Layer:
    """One decorator in a stack, outermost first.

    Attributes:
        role: Its role in `ROLES` (``"retry"``, ``"breaker"``...), or ``"outer"``,
            ``"inner"`` or ``"unknown"`` for decorators python-wrap cannot classify.
        settings: The configuration that matters for the checks (never secrets or
            client objects).
    """

    role: str
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        shown = ", ".join(
            f"{key}={_fmt(value)}"
            for key, value in self.settings.items()
            if value is not None and value is not False
        )
        return f"{self.role}({shown})" if shown else self.role


@dataclass(frozen=True)
class Finding:
    """A problem `doctor` found.

    Attributes:
        code: Stable identifier such as ``"PW001"`` (listed in the docs).
        severity: ``"error"`` (almost certainly wrong) or ``"warning"`` (review it).
        message: What is wrong and how to fix it.
    """

    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} {self.code}: {self.message}"


@dataclass(frozen=True)
class Report:
    """The layers of a stack and the findings about it.

    Attributes:
        target: What was inspected (policy or function name).
        layers: Layers outermost first.
        findings: Problems, errors first.
    """

    target: str
    layers: tuple[Layer, ...]
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """True when no finding has severity ``"error"``."""
        return not any(item.severity == "error" for item in self.findings)

    def __str__(self) -> str:
        lines = [f"{self.target}:"]
        lines += [f"  {index}. {layer}" for index, layer in enumerate(self.layers, 1)]
        if not self.layers:
            lines.append("  (no python-wrap layers)")
        lines += [f"  {finding}" for finding in self.findings] or ["  no findings"]
        return "\n".join(lines)


def _fmt(value: Any) -> str:
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, tuple):
        return "(" + ", ".join(_fmt(item) for item in value) + ")"
    if isinstance(value, float):
        return f"{value:g}"
    return repr(value)


def _layer(obj: Any, role: str | None = None) -> Layer:
    """Describe one decorator object or one wrapper function."""
    from python_wrap.bulkhead import Bulkhead
    from python_wrap.circuit import CircuitBreaker
    from python_wrap.concurrency import RateLimiter, Timeout

    tagged = getattr(obj, _LAYER_ATTR, None)
    if isinstance(tagged, tuple) and len(tagged) == 2:
        kind, value = tagged
        if isinstance(value, Mapping):
            return Layer(role or kind, dict(value))
        obj = value
    if isinstance(obj, Timeout):
        return Layer(role or "timeout", {"seconds": obj.seconds})
    if isinstance(obj, CircuitBreaker):
        return Layer(
            role or "breaker",
            {
                "name": obj.name,
                "failure_threshold": obj.failure_threshold,
                "recovery_timeout": obj.recovery_timeout,
                "shared": obj.backend is not None,
            },
        )
    if isinstance(obj, Bulkhead):
        return Layer(role or "bulkhead", {"max_concurrent": obj.max_concurrent})
    if isinstance(obj, RateLimiter):
        return Layer(role or "limiter", {"name": obj.name})
    if callable(getattr(obj, "cache_info", None)):
        return Layer(role or "cache")
    return Layer(role or "unknown", {"decorator": getattr(obj, "__qualname__", type(obj).__name__)})


def _check(layers: Sequence[Layer], idempotent: bool) -> tuple[Finding, ...]:
    found: list[Finding] = []
    roles = [layer.role for layer in layers]
    retries = [layer for layer in layers if layer.role == "retry"]
    timeouts = [layer for layer in layers if layer.role == "timeout"]
    for layer in retries:
        settings = layer.settings
        attempts, max_time = settings.get("max_attempts"), settings.get("max_time")
        if attempts is None and max_time is None:
            found.append(
                Finding(
                    "PW001",
                    "error",
                    "retry has neither max_attempts nor max_time: it retries forever. "
                    "Set max_attempts=, max_time= or both.",
                )
            )
        if not idempotent:
            found.append(
                Finding(
                    "PW002",
                    "error",
                    "retry wraps a non-idempotent operation: a retried POST/charge/send can "
                    "run twice. Remove the retry, or make the call idempotent (for example "
                    "with an Idempotency-Key) and then explain it with idempotent=True.",
                )
            )
        on = settings.get("on")
        broad = on in (Exception, BaseException) or (
            isinstance(on, tuple) and (Exception in on or BaseException in on)
        )
        if broad and not settings.get("retry_if"):
            found.append(
                Finding(
                    "PW003",
                    "warning",
                    "retry catches every Exception: programming errors (TypeError, KeyError) "
                    "are retried too. Narrow on= or add retry_if=, for example db_transient.",
                )
            )
        if not settings.get("budget"):
            found.append(
                Finding(
                    "PW008",
                    "warning",
                    "retry has no RetryBudget: during an outage every caller multiplies load "
                    "by max_attempts. Pass budget=RetryBudget(...) or configure(retry=...).",
                )
            )
    if retries and not timeouts:
        found.append(
            Finding(
                "PW004",
                "warning",
                "retry without a timeout: one hung attempt blocks the caller forever and "
                "the retry never runs. Add timeout=timeout(seconds).",
            )
        )
    if retries and timeouts:
        budget = retries[0].settings.get("max_time")
        limit = timeouts[-1].settings.get("seconds")
        if isinstance(budget, (int, float)) and isinstance(limit, (int, float)) and limit >= budget:
            found.append(
                Finding(
                    "PW005",
                    "warning",
                    f"the per-attempt timeout ({limit:g}s) is not shorter than the retry "
                    f"max_time ({budget:g}s): a single slow attempt uses the whole budget, "
                    "so there is never a second try.",
                )
            )
    known = [role for role in roles if role in ROLES]
    for earlier, later in itertools.pairwise(known):
        if ROLES.index(earlier) > ROLES.index(later):
            found.append(
                Finding(
                    "PW006",
                    "warning",
                    f"{earlier} is outside {later}; the canonical order puts {later} outside "
                    f"{earlier} ({' > '.join(ROLES)}). Use policy(...) to get the order right.",
                )
            )
    if roles.count("breaker") > 1 or roles.count("retry") > 1:
        found.append(
            Finding(
                "PW007",
                "warning",
                "the stack has more than one retry or breaker layer: attempts multiply "
                "(3 x 3 = 9 calls) and failures are counted twice.",
            )
        )
    found.sort(key=lambda item: (item.severity != "error", item.code))
    return tuple(found)


def explain(target: Any, *, idempotent: bool = True) -> Report:
    """Describe a `Policy` (or a decorated function) and lint its configuration.

    Args:
        target: A `Policy`, or a function decorated with python-wrap decorators (then
            this is the same as `doctor`).
        idempotent: Whether the protected operation is safe to repeat. Pass False for
            payments, emails, non-idempotent POSTs: any retry layer is then an error.

    Returns:
        A `Report`; ``str(report)`` is a readable summary and ``report.ok`` is False
        when an error was found.

    Example:
        ```python
        from python_wrap import policy, retry
        from python_wrap.doctor import explain

        risky = policy(retry=retry(max_attempts=3, on=ConnectionError), name="charge")
        report = explain(risky, idempotent=False)
        assert not report.ok
        assert [f.code for f in report.findings][:1] == ["PW002"]
        ```
    """
    if not isinstance(target, Policy):
        return doctor(target, idempotent=idempotent)
    layers: list[Layer] = [_layer(item, "outer") for item in target._outer]
    for role in ROLES:
        layers += [_layer(item, role) for item in target._roles.get(role, ())]
    layers += [_layer(item, "inner") for item in target._inner]
    # Reclassify outer/inner decorators python-wrap recognises: that is where a
    # misordered breaker or retry hides.
    layers = [
        _layer(item, None) if layer.role in {"outer", "inner"} else layer
        for layer, item in zip(layers, target._layers(), strict=True)
    ]
    return Report(target.name or repr(target), tuple(layers), _check(layers, idempotent))


def doctor(func: Callable[..., Any], *, idempotent: bool = True) -> Report:
    """Walk a decorated function's ``__wrapped__`` chain and lint the layers found.

    Only python-wrap layers are listed (plain ``functools.wraps`` decorators are
    skipped), outermost first.

    Args:
        func: The decorated function (or bound method).
        idempotent: See `explain`.

    Returns:
        A `Report`.

    Example:
        ```python
        from python_wrap import retry, timeout
        from python_wrap.doctor import doctor

        @retry(max_attempts=3, on=ConnectionError)
        @timeout(2)
        def fetch() -> str:
            return "ok"

        report = doctor(fetch)
        assert [layer.role for layer in report.layers] == ["retry", "timeout"]
        assert report.ok
        ```
    """
    current: Any = getattr(func, "__func__", func)
    name = getattr(current, "__qualname__", repr(current))
    layers: list[Layer] = []
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, _LAYER_ATTR):
            layers.append(_layer(current))
        elif callable(getattr(current, "cache_info", None)):
            layers.append(Layer("cache"))
        current = getattr(current, "__wrapped__", None)
    return Report(name, tuple(layers), _check(layers, idempotent))


def _resolve(spec: str) -> Any:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"expected module:attribute, got {spec!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m python_wrap doctor TARGET...``: print reports; exit 1 on any error."""
    parser = argparse.ArgumentParser(
        prog="python -m python_wrap doctor",
        description="Explain python-wrap resilience stacks and flag risky combinations.",
    )
    parser.add_argument("targets", nargs="+", help="module:attribute of a policy or function")
    parser.add_argument(
        "--non-idempotent",
        action="store_true",
        help="treat the targets as unsafe to repeat (any retry is an error)",
    )
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    args = parser.parse_args(argv)
    if "" not in sys.path:
        sys.path.insert(0, "")
    status = 0
    for spec in args.targets:
        report = explain(_resolve(spec), idempotent=not args.non_idempotent)
        print(report)
        if not report.ok or (args.strict and report.findings):
            status = 1
    return status
