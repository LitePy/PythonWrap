"""Make agent tool use safe: idempotent tools, run guards, permissions and approvals.

An LLM agent calls tools in a loop the model drives. Four things go wrong in practice:
a retried or replayed "send email" runs twice, the loop never ends (the model calls the
same tool with the same arguments again and again), a tool with side effects runs when it
should only have been planned, and an irreversible action runs without a human saying
yes. This module covers each one without depending on any agent framework or provider SDK:

* `tool` marks a function as an agent tool. Tools with side effects get an idempotency
  key derived from the tool name, the canonical arguments and the current run id, so a
  replayed call returns the stored result instead of running again.
* `RunGuard` / `agent_run` bound one agent run: steps, wall time, identical repeated
  calls and an optional budget. The run is visible through `current_run` and the
  `run_id_var` context variable, nests, works with ``with`` and ``async with``, and
  installs a `CancelToken` so `python_wrap.checkpoint` stops long tool code cooperatively.
* `ToolPolicy` enforces read-only runs, dry runs and allow/deny globs per run;
  `ToolRegistry` dispatches tool calls by name (as a model emits them).
* ``requires_approval=True`` asks a user-supplied approver; no answer, a timeout or an
  error denies.
* `EffectLedger` records every side effect as started / succeeded / failed, keyed on the
  provider's tool-call id (`with_call_id`) when there is one: replays return the stored
  result, concurrent duplicates run once, and a crash between the effect and the record
  is detected (`EffectUnknown`) and handed to a decision hook.

Everything publishes events (``agent.run.*``, ``tool.*``) through `python_wrap.events`.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import fnmatch
import functools
import hashlib
import inspect
import json
import math
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._runtime import seconds as _parse_seconds
from python_wrap.concurrency import CancelToken, _current_token
from python_wrap.errors import PythonWrapError, TimeoutExpired

__all__ = [
    "AgentError",
    "AgentRun",
    "ApprovalDenied",
    "ApprovalTimeout",
    "DryRun",
    "EffectConflict",
    "EffectLedger",
    "EffectRecord",
    "EffectResolved",
    "EffectStore",
    "EffectUnknown",
    "LoopDetected",
    "MemoryEffectStore",
    "RedisEffectStore",
    "RunBudgetExceeded",
    "RunCancelled",
    "RunGuard",
    "RunTimeout",
    "SQLiteEffectStore",
    "Step",
    "StepLimitExceeded",
    "ToolCall",
    "ToolNotAllowed",
    "ToolPolicy",
    "ToolRegistry",
    "ToolSpec",
    "agent_run",
    "current_run",
    "record_step",
    "run_id_var",
    "tool",
    "tool_call_id",
    "tool_spec",
    "with_call_id",
]

P = ParamSpec("P")
R = TypeVar("R")

_LAYER_ATTR = "__python_wrap_layer__"
_SPEC_ATTR = "__python_wrap_tool__"
_HISTORY_LIMIT = 1000


# --------------------------------------------------------------------------- errors


class AgentError(PythonWrapError):
    """Base class for the errors this module raises.

    Attributes:
        run_id: The run the error belongs to, or None outside a run.
    """

    run_id: str | None = None


class StepLimitExceeded(AgentError):
    """The run already made ``max_steps`` steps; this one was refused.

    Attributes:
        limit: The ``max_steps`` that was reached.
    """

    def __init__(self, message: str, *, run_id: str, limit: int) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.limit = limit


class LoopDetected(AgentError):
    """The same call (same kind, name and arguments) was made too many times in one run.

    Attributes:
        name: The tool (or step) name that repeats.
        repeats: How many identical calls had already been made.
    """

    def __init__(self, message: str, *, run_id: str, name: str, repeats: int) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.name = name
        self.repeats = repeats


class RunTimeout(AgentError, TimeoutExpired):
    """The run is older than ``max_seconds``. Also a `TimeoutExpired` (and ``TimeoutError``)."""

    def __init__(self, message: str, *, run_id: str, seconds: float) -> None:
        super().__init__(message, seconds=seconds)
        self.run_id = run_id


class RunCancelled(AgentError):
    """`AgentRun.cancel` was called; no further step is admitted."""

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class RunBudgetExceeded(AgentError):
    """The run's ``budget`` reports ``exhausted``."""

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class ToolNotAllowed(AgentError):
    """A `ToolPolicy` (or a missing tool) refused the call.

    Attributes:
        tool: The tool name.
        reason: ``"unknown"``, ``"denied"`` (deny glob / not in the allow list),
            ``"read_only"`` (a write tool in a run with ``allow_write=False``),
            ``"approval_denied"`` or ``"approval_timeout"``.
    """

    def __init__(self, message: str, *, tool: str, reason: str, run_id: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.reason = reason
        self.run_id = run_id


class ApprovalDenied(ToolNotAllowed):
    """The approver said no, raised, returned something other than True, or none is set."""


class ApprovalTimeout(ApprovalDenied):
    """The approver did not answer within ``approval_timeout`` seconds (denied)."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Step:
    """One step of an agent run (a tool call, an LLM call, or a custom kind).

    Attributes:
        index: 1-based position in the run.
        kind: ``"tool"``, ``"llm"`` or any label passed to `record_step`.
        name: Tool or model name.
        signature: A digest of the canonical arguments (None when none were given);
            identical calls share it.
        at: Seconds since the run started.
    """

    index: int
    kind: str
    name: str
    signature: str | None
    at: float


@dataclass(frozen=True)
class ToolCall:
    """What an approver (and `DryRun`) is told about a pending tool call.

    Attributes:
        tool: The tool name.
        arguments: The bound arguments, defaults applied (read-only mapping).
        run_id: The current run id, or None outside a run.
        read_only: Whether the tool declared itself read-only.
        description: The tool's description (first docstring line by default).
    """

    tool: str
    arguments: Mapping[str, Any]
    run_id: str | None
    read_only: bool
    description: str = ""
    call_id: str | None = None


@dataclass(frozen=True)
class DryRun:
    """Returned instead of running a write tool in a dry run without a ``dry_result``.

    Attributes:
        tool: The tool that would have run.
        arguments: The arguments it would have received.
    """

    tool: str
    arguments: Mapping[str, Any]

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True)
class ToolSpec:
    """What `tool` recorded about a function; returned by `tool_spec`.

    Attributes:
        name: The tool name the model uses.
        description: First docstring line unless given.
        read_only: True for tools without side effects.
        idempotent: True when replays are deduplicated with an idempotency key.
        requires_approval: True when every call needs the approver's yes.
        timeout: Per-call time limit in seconds, or None.
        retried: True when the tool was given ``retry=``.
        is_async: True for ``async def`` tools.
    """

    name: str
    description: str
    read_only: bool
    idempotent: bool
    requires_approval: bool
    timeout: float | None
    retried: bool
    is_async: bool


Approver = Callable[[ToolCall], "bool | Awaitable[bool]"]


def _globs(value: Iterable[str] | str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class ToolPolicy:
    """Which tools a run may call, and how.

    Policies are attached to a run (``agent_run(policy=...)``) or a `ToolRegistry`. Every
    policy in effect must agree: nested runs and the registry combine, and the strictest
    answer wins (any deny, any ``allow_write=False``, any ``dry_run=True``).

    Args:
        allow: Glob patterns (``fnmatch``, case-sensitive) of tool names that may run;
            None allows every name.
        deny: Glob patterns that may never run; deny wins over allow.
        allow_write: False refuses every tool not declared ``read_only=True``
            (`ToolNotAllowed` with reason ``"read_only"``).
        dry_run: True returns each write tool's ``dry_result`` (or a `DryRun` marker)
            instead of running it. Read-only tools still run, so the agent can plan with
            real data.
        approver: Called with a `ToolCall` for tools with ``requires_approval=True``;
            sync or async, it must return exactly True to approve.
        approval_timeout: Seconds to wait for the approver before denying.

    Example:
        >>> policy = ToolPolicy(deny=["delete_*"], allow_write=False)
        >>> policy.permits("search_docs"), policy.permits("delete_user")
        (True, False)
    """

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()
    allow_write: bool = True
    dry_run: bool = False
    approver: Approver | None = None
    approval_timeout: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "allow", _globs(self.allow))
        object.__setattr__(self, "deny", _globs(self.deny) or ())
        object.__setattr__(self, "approval_timeout", _parse_seconds(self.approval_timeout))
        if self.approver is not None and not callable(self.approver):
            raise TypeError("approver must be callable")

    def permits(self, name: str) -> bool:
        """True if ``name`` passes the allow and deny globs."""
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.deny):
            return False
        return self.allow is None or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in self.allow
        )


# --------------------------------------------------------------------------- runs

#: The id of the innermost agent run in this context, or None. Tracing integrations can
#: read it without importing anything else from this module.
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "python_wrap_agent_run_id", default=None
)
_run_var: contextvars.ContextVar[AgentRun | None] = contextvars.ContextVar(
    "python_wrap_agent_run", default=None
)
# A registry's policy, in effect while `ToolRegistry.call` runs a tool.
_registry_policy: contextvars.ContextVar[ToolPolicy | None] = contextvars.ContextVar(
    "python_wrap_agent_registry_policy", default=None
)


def canonical(arguments: Mapping[str, Any] | None) -> str | None:
    """A stable digest of call arguments: equal arguments give equal digests.

    Keys are sorted and values serialised as JSON (sets sorted, unknown objects by
    ``repr``), so ``f(a=1, b=2)`` and ``f(b=2, a=1)`` match.
    """
    if arguments is None:
        return None

    def fallback(obj: Any) -> Any:
        if isinstance(obj, (set, frozenset)):
            return sorted(obj, key=repr)
        if isinstance(obj, (bytes, bytearray)):
            return obj.hex()
        return repr(obj)

    try:
        text = json.dumps(
            dict(arguments),
            sort_keys=True,
            default=fallback,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        text = repr(sorted((repr(k), repr(v)) for k, v in arguments.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _positive_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


class AgentRun:
    """The live state of one agent run; get it from `current_run` or ``with guard as run``.

    Thread-safe: parallel tool calls (threads or tasks started inside the run) share it.

    Attributes:
        run_id: This run's id (``uuid4().hex`` unless the guard was given ``run_id=``).
        parent: The enclosing run, or None.
        guard: The `RunGuard` that started it.
        token: The run's `CancelToken`, installed as the current token, so
            `python_wrap.checkpoint` inside the run raises once the run is cancelled or
            its ``max_seconds`` pass.
    """

    def __init__(self, guard: RunGuard, run_id: str, parent: AgentRun | None) -> None:
        self.guard = guard
        self.run_id = run_id
        self.parent = parent
        self.token = CancelToken(
            f"agent run {run_id}", guard.max_seconds or 0.0, _current_token.get()
        )
        self._lock = threading.Lock()
        self._steps = 0
        self._history: list[Step] = []
        self._counts: dict[tuple[str, str, str], int] = {}
        self._started = _core.monotonic()
        self._timed_out = False
        self._user_cancelled = False
        self._resets: tuple[Any, ...] = ()
        self._timer: Any = None
        self._scope: Any = None

    @property
    def steps(self) -> int:
        """Steps admitted so far (tool calls, LLM calls, custom steps)."""
        with self._lock:
            return self._steps

    @property
    def history(self) -> tuple[Step, ...]:
        """The admitted steps, oldest first (the last 1000 are kept)."""
        with self._lock:
            return tuple(self._history)

    @property
    def elapsed(self) -> float:
        """Seconds since the run started."""
        return _core.monotonic() - self._started

    @property
    def remaining(self) -> float | None:
        """Seconds left before ``max_seconds`` (never negative), or None without a limit."""
        limit = self.guard.max_seconds
        return None if limit is None else max(0.0, limit - self.elapsed)

    @property
    def cancelled(self) -> bool:
        """True once the run was cancelled, timed out, or an enclosing token fired."""
        return self.token.cancelled or self._over_time()

    def cancel(self) -> None:
        """Stop the run: the next step raises `RunCancelled` and `checkpoint` raises."""
        self._user_cancelled = True
        self.token.cancel()

    def _expire(self) -> None:
        self._timed_out = True
        self.token.cancel()

    def _over_time(self) -> bool:
        limit = self.guard.max_seconds
        return limit is not None and self.elapsed >= limit

    def check(self) -> None:
        """Raise if this run (or an enclosing one) may not continue; else do nothing.

        Raises:
            RunTimeout: ``max_seconds`` passed.
            RunCancelled: `cancel` was called.
            RunBudgetExceeded: the budget is exhausted.
            TimeoutExpired: an enclosing `timeout` fired.
        """
        for run in self._chain():
            run._check_own()
        self.token.raise_if_cancelled()

    def _check_own(self) -> None:
        if self._timed_out or self._over_time():
            self._expire()
            limit = self.guard.max_seconds or 0.0
            raise RunTimeout(
                f"agent run {self.run_id} exceeded max_seconds={limit:g}",
                run_id=self.run_id,
                seconds=limit,
            )
        if self._user_cancelled:
            raise RunCancelled(f"agent run {self.run_id} was cancelled", run_id=self.run_id)
        budget = self.guard.budget
        if _is_llm_budget(budget):
            if self._scope is not None:
                over = budget._over(self._scope.spent)
            else:
                over = budget.per == "total" and budget.exhausted()
            if over:
                raise RunBudgetExceeded(
                    f"agent run {self.run_id}: budget {budget.name!r} exhausted",
                    run_id=self.run_id,
                )
        elif budget is not None:
            checker = getattr(budget, "check", None)
            if callable(checker):
                checker()
            if getattr(budget, "exhausted", False) is True:
                raise RunBudgetExceeded(
                    f"agent run {self.run_id}: budget exhausted", run_id=self.run_id
                )

    def _chain(self) -> list[AgentRun]:
        """This run and its ancestors, outermost first."""
        chain: list[AgentRun] = []
        run: AgentRun | None = self
        while run is not None:
            chain.append(run)
            run = run.parent
        chain.reverse()
        return chain

    def step(self, kind: str, name: str, arguments: Mapping[str, Any] | None = None) -> Step:
        """Admit and record one step; see `record_step`."""
        return self._record(kind, name, canonical(arguments))

    def _record(self, kind: str, name: str, signature: str | None) -> Step:
        chain = self._chain()
        self.check()
        key = (kind, name, signature) if signature is not None else None
        with ExitStack() as stack:
            for run in chain:
                stack.enter_context(run._lock)
            for run in chain:
                limit, repeats = run.guard.max_steps, run.guard.max_repeats
                if limit is not None and run._steps >= limit:
                    raise StepLimitExceeded(
                        f"agent run {run.run_id} reached max_steps={limit}; "
                        f"refused {kind} step {name!r}",
                        run_id=run.run_id,
                        limit=limit,
                    )
                if key is not None and repeats is not None:
                    seen = run._counts.get(key, 0)
                    if seen >= repeats:
                        raise LoopDetected(
                            f"agent run {run.run_id}: {kind} {name!r} was already called "
                            f"{seen} time(s) with the same arguments (max_repeats={repeats})",
                            run_id=run.run_id,
                            name=name,
                            repeats=seen,
                        )
            mine: Step | None = None
            for run in chain:
                run._steps += 1
                if key is not None:
                    run._counts[key] = run._counts.get(key, 0) + 1
                entry = Step(run._steps, kind, name, signature, run.elapsed)
                run._history.append(entry)
                if len(run._history) > _HISTORY_LIMIT:
                    del run._history[0]
                mine = entry
        assert mine is not None
        _core.emit(
            "agent.run.step",
            self.guard.name,
            run_id=self.run_id,
            step=mine.index,
            kind=kind,
            tool=name,
        )
        return mine

    def __repr__(self) -> str:
        return f"AgentRun(run_id={self.run_id!r}, steps={self.steps})"


def current_run() -> AgentRun | None:
    """The innermost `AgentRun` active in this context, or None.

    Example:
        >>> with agent_run(max_steps=5, run_id="demo") as run:
        ...     current_run() is run, current_run().run_id
        (True, 'demo')
        >>> current_run() is None
        True
    """
    return _run_var.get()


def record_step(kind: str, name: str, arguments: Mapping[str, Any] | None = None) -> Step | None:
    """Count one step (an LLM call, say) against the current run's limits.

    `tool` records tool calls itself; call this for the other steps of the loop.
    Outside a run it does nothing and returns None.

    Args:
        kind: ``"llm"`` or any label.
        name: Model or step name.
        arguments: When given, identical calls count towards ``max_repeats``.

    Returns:
        The recorded `Step`, or None outside a run.

    Raises:
        StepLimitExceeded, LoopDetected, RunTimeout, RunCancelled, RunBudgetExceeded: The
            step is refused (it is not recorded).
    """
    run = _run_var.get()
    return None if run is None else run.step(kind, name, arguments)


def _is_llm_budget(budget: Any) -> bool:
    from python_wrap.llm import Budget

    return isinstance(budget, Budget)


class RunGuard:
    """Limits for an agent run; enter it (``with`` / ``async with``) or decorate a loop.

    Each entry starts a new `AgentRun`, so one guard can be reused. Runs nest: steps of an
    inner run count against the enclosing run's limits too, so a sub-agent cannot get
    around them. The run id is published through `current_run` and `run_id_var`, and
    the run's `CancelToken` becomes the current token, so `python_wrap.checkpoint` stops
    tool code once ``max_seconds`` pass or the run is cancelled.

    Args:
        max_steps: Most steps (tool + LLM + custom) per run; the next raises
            `StepLimitExceeded`.
        max_seconds: Wall-time limit (seconds or ``"5m"``); the next step raises
            `RunTimeout` and the token is cancelled when it passes.
        max_repeats: Most identical calls (same kind, name and arguments) per run; the
            next raises `LoopDetected`.
        budget: Optional spend tracker checked before every step: its ``check()`` is
            called when it has one (it may raise), and ``exhausted`` being True raises
            `RunBudgetExceeded`.
        policy: A `ToolPolicy` in effect for the run.
        run_id: Fixed run id (resume a run so its tool replays are deduplicated);
            a fresh ``uuid4().hex`` per run by default.
        name: Reported as ``Event.function`` of the ``agent.run.*`` events.

    Example:
        >>> guard = RunGuard(max_steps=2)
        >>> with guard as run:
        ...     _ = record_step("llm", "model")
        ...     _ = record_step("llm", "model")
        ...     try:
        ...         record_step("llm", "model")
        ...     except StepLimitExceeded:
        ...         print("stopped after", run.steps)
        stopped after 2
    """

    def __init__(
        self,
        *,
        max_steps: int | None = None,
        max_seconds: float | str | None = None,
        max_repeats: int | None = None,
        budget: Any = None,
        policy: ToolPolicy | None = None,
        run_id: str | None = None,
        name: str = "agent_run",
    ) -> None:
        self.max_steps = _positive_int("max_steps", max_steps)
        self.max_repeats = _positive_int("max_repeats", max_repeats)
        self.max_seconds = None if max_seconds is None else _parse_seconds(max_seconds)
        if policy is not None and not isinstance(policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise ValueError("run_id must be a non-empty string")
        self.budget = budget
        self.policy = policy
        self.run_id = run_id
        self.name = name

    @property
    def limited(self) -> bool:
        """True if at least one of max_steps, max_seconds, max_repeats is set."""
        return any(v is not None for v in (self.max_steps, self.max_seconds, self.max_repeats))

    def settings(self) -> dict[str, Any]:
        """The limits, as `python_wrap.doctor` reports them."""
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_repeats": self.max_repeats,
            "budget": self.budget is not None,
        }

    def _open(self) -> AgentRun:
        parent = _run_var.get()
        run = AgentRun(self, self.run_id or uuid.uuid4().hex, parent)
        run._resets = (
            _run_var.set(run),
            run_id_var.set(run.run_id),
            _current_token.set(run.token),
        )
        if _is_llm_budget(self.budget) and self.budget.per in ("run", "chain"):
            run._scope = self.budget.run()
            run._scope.__enter__()
        _core.emit(
            "agent.run.start",
            self.name,
            run_id=run.run_id,
            parent_id=None if parent is None else parent.run_id,
        )
        return run

    def _close(self, exc: BaseException | None) -> None:
        run = _run_var.get()
        if run is None or run.guard is not self:
            raise RuntimeError("RunGuard exited out of order")
        timer, run._timer = run._timer, None
        if timer is not None:
            timer.cancel()
        scope, run._scope = run._scope, None
        if scope is not None:
            scope.__exit__(None, None, None)
        run_token, id_token, cancel_token = run._resets
        _current_token.reset(cancel_token)
        run_id_var.reset(id_token)
        _run_var.reset(run_token)
        _core.emit(
            "agent.run.end",
            self.name,
            run_id=run.run_id,
            steps=run._steps,
            seconds=run.elapsed,
            error=None if exc is None else type(exc).__name__,
        )

    def __enter__(self) -> AgentRun:
        run = self._open()
        if self.max_seconds is not None:
            timer = threading.Timer(self.max_seconds, run._expire)
            timer.daemon = True
            timer.start()
            run._timer = timer
        return run

    def __exit__(
        self,
        typ: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close(exc)

    async def __aenter__(self) -> AgentRun:
        run = self._open()
        if self.max_seconds is not None:
            run._timer = asyncio.get_running_loop().call_later(self.max_seconds, run._expire)
        return run

    async def __aexit__(
        self,
        typ: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close(exc)

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Run every call of ``func`` (sync or async) inside a fresh run of this guard."""
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with self:
                    return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)

            setattr(async_wrapper, _LAYER_ATTR, ("agent_run", self.settings()))
            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with self:
                return func(*args, **kwargs)

        setattr(wrapper, _LAYER_ATTR, ("agent_run", self.settings()))
        return wrapper

    def _with_policy(self, policy: ToolPolicy | None) -> RunGuard:
        clone = RunGuard(
            max_steps=self.max_steps,
            max_seconds=self.max_seconds,
            max_repeats=self.max_repeats,
            budget=self.budget,
            policy=self.policy,
            run_id=self.run_id,
            name=self.name,
        )
        clone._extra_policy = policy
        return clone

    _extra_policy: ToolPolicy | None = None

    def _explain(self, *, idempotent: bool = True, multiprocess: bool = False) -> Any:
        from python_wrap.doctor import Layer, Report, _check

        layers = (Layer("agent_run", self.settings()),)
        return Report(self.name, layers, _check(layers, idempotent, multiprocess))

    def __repr__(self) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in self.settings().items() if v)
        return f"RunGuard({shown})"


def agent_run(
    *,
    max_steps: int | None = None,
    max_seconds: float | str | None = None,
    max_repeats: int | None = None,
    budget: Any = None,
    policy: ToolPolicy | None = None,
    run_id: str | None = None,
    name: str = "agent_run",
) -> RunGuard:
    """Shorthand for `RunGuard`: ``with agent_run(max_steps=20) as run: ...``.

    Example:
        ```python
        from python_wrap import agent_run, tool, LoopDetected

        @tool(read_only=True)
        def search(query: str) -> list[str]:
            return [query.upper()]

        with agent_run(max_steps=10, max_repeats=2) as run:
            search("python")
            search("python")
            try:
                search("python")
            except LoopDetected:
                pass
        assert run.steps == 2
        ```
    """
    return RunGuard(
        max_steps=max_steps,
        max_seconds=max_seconds,
        max_repeats=max_repeats,
        budget=budget,
        policy=policy,
        run_id=run_id,
        name=name,
    )


# --------------------------------------------------------------------------- effect ledger

#: The provider's id for the tool call being executed (OpenAI ``tool_call.id``, Anthropic
#: ``tool_use.id``, pydantic-ai ``tool_call_id``), or None. Adapters set it with
#: `with_call_id`; an `EffectLedger` keys side effects on it.
tool_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "python_wrap_tool_call_id", default=None
)


@contextlib.contextmanager
def with_call_id(call_id: str | None) -> Iterator[str | None]:
    """Make ``call_id`` the current `tool_call_id` for a ``with`` block.

    Framework adapters do this for you; call it yourself when you dispatch the model's
    tool calls by hand (``with with_call_id(block.id): tools.call(...)``).

    Example:
        >>> with with_call_id("call_1"):
        ...     tool_call_id.get()
        'call_1'
        >>> tool_call_id.get() is None
        True
    """
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        raise ValueError("call_id must be a non-empty string or None")
    reset = tool_call_id.set(call_id)
    try:
        yield call_id
    finally:
        tool_call_id.reset(reset)


@dataclass(frozen=True)
class EffectRecord:
    """What an `EffectLedger` knows about one side effect.

    Attributes:
        key: The ledger key (tool name plus call id, business key or argument digest).
        tool: The tool name.
        state: ``"started"`` (claimed, no outcome recorded yet), ``"succeeded"`` or
            ``"failed"``.
        owner: A unique id of the attempt that holds (or held) the record.
        call_id: The provider's tool-call id, when one was current.
        arguments: Digest of the canonical arguments.
        run_id: The agent run the call belonged to.
        started_at: Wall-clock time the attempt was claimed.
        lease_until: Wall-clock time after which a ``"started"`` record whose owner never
            reported back is treated as crashed.
        finished_at: Wall-clock time the outcome was recorded, or None.
        result: The stored result of a succeeded call (when ``has_result``).
        has_result: False when the result could not be stored (not serialisable).
        error: ``{"type": ..., "message": ...}`` of a failed call.
        uncertain: True for a failure that may have left the effect half done (a timeout,
            a cancellation): such a record is handled like a crash.
        attempts: How many attempts claimed this key.
    """

    key: str
    tool: str
    state: str
    owner: str
    call_id: str | None = None
    arguments: str | None = None
    run_id: str | None = None
    started_at: float = 0.0
    lease_until: float = 0.0
    finished_at: float | None = None
    result: Any = None
    has_result: bool = False
    error: dict[str, str] | None = None
    uncertain: bool = False
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        """A plain dict (JSON-serialisable when the result is)."""
        data = {f: getattr(self, f) for f in _RECORD_FIELDS}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EffectRecord:
        """Rebuild a record from `to_dict` output; unknown keys are ignored."""
        return cls(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})


_RECORD_FIELDS = (
    "key", "tool", "state", "owner", "call_id", "arguments", "run_id", "started_at",
    "lease_until", "finished_at", "result", "has_result", "error", "uncertain", "attempts",
)  # fmt: skip


@dataclass(frozen=True)
class EffectResolved:
    """Returned by an ``on_unknown`` hook: the effect did happen and this is its result.

    The ledger records ``value`` as the call's result and returns it, without running
    the tool again.
    """

    value: Any = None


class EffectUnknown(AgentError):
    """A side effect may or may not have happened, and no hook decided what to do.

    Raised when a previous attempt claimed the key but never recorded an outcome (the
    process crashed, or the ledger store failed between the effect and the record), when
    it failed in a way that may have left the effect half done (a timeout or a
    cancellation), or when it succeeded but its result could not be stored.

    Attributes:
        record: The ledger's `EffectRecord`.
        reason: ``"crashed"``, ``"uncertain"`` or ``"result_lost"``.
    """

    def __init__(self, message: str, *, record: EffectRecord, reason: str) -> None:
        super().__init__(message)
        self.record = record
        self.reason = reason
        self.run_id = record.run_id


class EffectConflict(AgentError):
    """A tool-call id was reused with different arguments: a bug in the caller.

    Attributes:
        record: The record stored for the first use of the id.
    """

    def __init__(self, message: str, *, record: EffectRecord) -> None:
        super().__init__(message)
        self.record = record
        self.run_id = record.run_id


class EffectStore(Protocol):
    """Where an `EffectLedger` keeps its records; implement it for another backend.

    ``claim`` and ``settle`` must be atomic with respect to every other process sharing
    the store.
    """

    def get(self, key: str, /) -> EffectRecord | None:
        """The live record for ``key``, or None."""

    def claim(self, record: EffectRecord, ttl: float, expect: str | None, /) -> EffectRecord | None:
        """Write ``record`` if no live record exists or the live one's owner is ``expect``.

        Returns None when written, otherwise the live record that blocked the write.
        """

    def settle(self, record: EffectRecord, ttl: float, /) -> bool:
        """Overwrite the live record only if its owner is ``record.owner``; True if written."""

    def delete(self, key: str, /) -> bool:
        """Forget ``key``; True if something was deleted."""


class MemoryEffectStore:
    """An in-process `EffectStore`: thread-safe, bounded, expiring.

    Use it for a single process. Records survive a crash only in a shared store
    (`SQLiteEffectStore`, `RedisEffectStore`).

    Args:
        max_entries: Most records kept; when full the record closest to expiry goes first.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._data: dict[str, tuple[EffectRecord, float]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _live(self, key: str) -> EffectRecord | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry[1] <= _core.wall_time():
            del self._data[key]
            return None
        return entry[0]

    def _put(self, record: EffectRecord, ttl: float) -> None:
        now = _core.wall_time()
        self._data.pop(record.key, None)
        if len(self._data) >= self.max_entries:
            for old in [k for k, (_, expires) in self._data.items() if expires <= now]:
                del self._data[old]
        while len(self._data) >= self.max_entries:
            del self._data[min(self._data, key=lambda k: self._data[k][1])]
        self._data[record.key] = (record, now + ttl)

    def get(self, key: str, /) -> EffectRecord | None:
        with self._lock:
            return self._live(key)

    def claim(self, record: EffectRecord, ttl: float, expect: str | None, /) -> EffectRecord | None:
        with self._lock:
            current = self._live(record.key)
            if current is not None and current.owner != expect:
                return current
            self._put(record, ttl)
            return None

    def settle(self, record: EffectRecord, ttl: float, /) -> bool:
        with self._lock:
            current = self._live(record.key)
            if current is None or current.owner != record.owner:
                return False
            self._put(record, ttl)
            return True

    def delete(self, key: str, /) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None


def _json_dumps(data: Any) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()


def _json_loads(data: bytes | str) -> Any:
    return json.loads(data)


class SQLiteEffectStore:
    """An `EffectStore` in a local SQLite file, shared by every process that opens it.

    Records survive a crash and a restart, so a replayed run finds them. Results are
    serialised with ``dumps`` (JSON by default: a result JSON cannot encode is not
    stored, and replaying it raises `EffectUnknown` with reason ``"result_lost"``
    unless the ``on_unknown`` hook resolves it).

    Args:
        path: The database file (created if missing).
        dumps: Serialiser for a record dict (bytes or str).
        loads: Matching deserialiser.
        timeout: Seconds to wait for another process's write lock.
    """

    def __init__(
        self,
        path: str | Any,
        *,
        dumps: Callable[[Any], bytes | str] = _json_dumps,
        loads: Callable[[Any], Any] = _json_loads,
        timeout: float = 30.0,
    ) -> None:
        import os

        self.path = os.fspath(path)
        self.dumps, self.loads, self.timeout = dumps, loads, timeout
        self._lock = threading.Lock()
        self._conn: Any = None
        self._pid = 0
        with self._lock:
            self._connection()

    def _connection(self) -> Any:
        import os
        import sqlite3

        if self._conn is None or self._pid != os.getpid():
            conn = sqlite3.connect(
                self.path, timeout=self.timeout, check_same_thread=False, isolation_level=None
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS python_wrap_effects (key TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, expires REAL NOT NULL, data BLOB NOT NULL)"
            )
            self._conn, self._pid = conn, os.getpid()
        return self._conn

    def _decode(self, row: Any) -> EffectRecord | None:
        if row is None or row[1] <= _core.wall_time():
            return None
        return EffectRecord.from_dict(self.loads(row[0]))

    def get(self, key: str, /) -> EffectRecord | None:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT data, expires FROM python_wrap_effects WHERE key = ?", (key,))
                .fetchone()
            )
            return self._decode(row)

    def claim(self, record: EffectRecord, ttl: float, expect: str | None, /) -> EffectRecord | None:
        data = self.dumps(record.to_dict())
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data, expires, owner FROM python_wrap_effects WHERE key = ?",
                    (record.key,),
                ).fetchone()
                current = self._decode(row)
                if current is not None and row[2] != expect:
                    conn.execute("COMMIT")
                    return current
                conn.execute(
                    "INSERT OR REPLACE INTO python_wrap_effects (key, owner, expires, data) "
                    "VALUES (?, ?, ?, ?)",
                    (record.key, record.owner, _core.wall_time() + ttl, data),
                )
                conn.execute("COMMIT")
                return None
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def settle(self, record: EffectRecord, ttl: float, /) -> bool:
        data = self.dumps(record.to_dict())
        with self._lock:
            cursor = self._connection().execute(
                "UPDATE python_wrap_effects SET data = ?, expires = ? "
                "WHERE key = ? AND owner = ? AND expires > ?",
                (data, _core.wall_time() + ttl, record.key, record.owner, _core.wall_time()),
            )
            return bool(cursor.rowcount == 1)

    def delete(self, key: str, /) -> bool:
        with self._lock:
            cursor = self._connection().execute(
                "DELETE FROM python_wrap_effects WHERE key = ?", (key,)
            )
            return bool(cursor.rowcount == 1)

    def close(self) -> None:
        """Close this process's connection (reopened on next use)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


_REDIS_CLAIM = """
local o=redis.call('HGET',KEYS[1],'owner')
if (not o) or (ARGV[1]~='' and o==ARGV[1]) then
 redis.call('DEL',KEYS[1])
 redis.call('HSET',KEYS[1],'owner',ARGV[2],'data',ARGV[3])
 redis.call('PEXPIRE',KEYS[1],ARGV[4])
 return false
end
return redis.call('HGET',KEYS[1],'data')
"""

_REDIS_SETTLE = """
if redis.call('HGET',KEYS[1],'owner')==ARGV[1] then
 redis.call('HSET',KEYS[1],'data',ARGV[2])
 redis.call('PEXPIRE',KEYS[1],ARGV[3])
 return 1
end
return 0
"""


class RedisEffectStore:
    """An `EffectStore` on Redis, shared by every worker; redis is never imported.

    Each record is one hash updated by a Lua script, so a claim and an outcome are
    atomic and an outcome is only written by the attempt that owns the record (a stale
    owner whose lease was taken over cannot overwrite the new one).

    Args:
        client: A synchronous ``redis.Redis``-compatible client (``eval``, ``hget``,
            ``delete``).
        prefix: Namespace for the keys.
        dumps: Serialiser for a record dict (JSON by default).
        loads: Matching deserialiser.
    """

    def __init__(
        self,
        client: Any,
        prefix: str = "python-wrap:effects:",
        *,
        dumps: Callable[[Any], bytes | str] = _json_dumps,
        loads: Callable[[Any], Any] = _json_loads,
    ) -> None:
        if not prefix:
            raise ValueError("RedisEffectStore needs a nonempty prefix")
        self.client, self.prefix, self.dumps, self.loads = client, prefix, dumps, loads

    @classmethod
    def from_store(cls, store: Any) -> RedisEffectStore:
        """Reuse a `python_wrap.stores.RedisStore`'s client, prefix and serialisers."""
        if getattr(store, "fail_open", False):
            raise ValueError("an effect ledger requires a RedisStore with fail_open=False")
        return cls(store.client, store.prefix + "effects:", dumps=store.dumps, loads=store.loads)

    def _key(self, key: str) -> str:
        return self.prefix + key

    def _decode(self, data: Any) -> EffectRecord | None:
        return None if data is None else EffectRecord.from_dict(self.loads(data))

    def get(self, key: str, /) -> EffectRecord | None:
        return self._decode(self.client.hget(self._key(key), "data"))

    def claim(self, record: EffectRecord, ttl: float, expect: str | None, /) -> EffectRecord | None:
        found = self.client.eval(
            _REDIS_CLAIM,
            1,
            self._key(record.key),
            expect or "",
            record.owner,
            self.dumps(record.to_dict()),
            max(1, math.ceil(ttl * 1000)),
        )
        return None if found is None or found is False else self._decode(found)

    def settle(self, record: EffectRecord, ttl: float, /) -> bool:
        done = self.client.eval(
            _REDIS_SETTLE,
            1,
            self._key(record.key),
            record.owner,
            self.dumps(record.to_dict()),
            max(1, math.ceil(ttl * 1000)),
        )
        return bool(int(done or 0))

    def delete(self, key: str, /) -> bool:
        return bool(self.client.delete(self._key(key)))


def _effect_store(store: Any) -> EffectStore:
    if store is None:
        return MemoryEffectStore()
    if all(callable(getattr(store, name, None)) for name in ("get", "claim", "settle", "delete")):
        return cast(EffectStore, store)
    from python_wrap.stores import RedisStore

    if isinstance(store, RedisStore):
        return RedisEffectStore.from_store(store)
    if isinstance(store, str) or hasattr(store, "__fspath__"):
        return SQLiteEffectStore(store)
    raise TypeError(
        "store must be an EffectStore, a RedisStore(fail_open=False), a SQLite file path or None"
    )


UnknownHook = Callable[[EffectUnknown], Any]


class EffectLedger:
    """Exactly-once bookkeeping for agent tool side effects, keyed on the tool-call id.

    Every call claims a record (``started``) before the tool runs and records the
    outcome (``succeeded`` with the result, or ``failed``) after. So:

    * A replay of the same call (the provider's ``call_id`` when one is current, see
      `with_call_id`; otherwise the tool name, run id and argument digest) returns the
      stored result without running the tool.
    * Concurrent identical calls run the tool once; the others wait and get its result.
    * A ``started`` record nobody finished (the process died between the side effect
      and the record, or the store failed while recording) is detected once its lease
      runs out, or at once in the process that saw the store fail. The ``on_unknown``
      hook then decides: return ``"retry"`` to run again, an `EffectResolved` to record
      the outcome you looked up yourself, or ``"raise"`` / None to raise `EffectUnknown`.
    * A failure that may have left the effect half done (``TimeoutError``, cancellation)
      goes through the same hook; any other failure lets the next call run again.

    Args:
        store: `MemoryEffectStore` (default), `SQLiteEffectStore`, `RedisEffectStore`, a
            `python_wrap.stores.RedisStore` with ``fail_open=False``, or a file path
            (SQLite). Anything implementing `EffectStore` works.
        ttl: How long records (and replayable results) are kept (``"1d"``).
        lease: How long a ``started`` record is trusted before it counts as crashed;
            make it longer than the tool's slowest call (``"5m"``).
        on_unknown: The decision hook (sync or async), called with the `EffectUnknown`.
        wait_timeout: Most seconds a duplicate waits for an in-flight call before
            raising `TimeoutExpired`; None waits until the lease runs out.
        uncertain: Exception types treated as "the effect may be half done";
            exceptions that are not ``Exception`` subclasses always are.
        poll: Seconds between store reads while waiting for another process.

    Example:
        ```python
        from python_wrap import EffectLedger, agent_run, tool, with_call_id

        ledger = EffectLedger()
        charges = []

        @tool(ledger=ledger)
        def charge(order: str, cents: int) -> str:
            charges.append(order)
            return f"charged {order}"

        with agent_run():
            with with_call_id("call_1"):
                charge("A-1", 500)
                charge("A-1", 500)  # a replay of call_1: stored result
            with with_call_id("call_2"):
                charge("A-1", 500)  # a new call from the model: runs
        assert charges == ["A-1", "A-1"]
        ```
    """

    def __init__(
        self,
        store: Any = None,
        *,
        ttl: float | str = "1d",
        lease: float | str = "5m",
        on_unknown: UnknownHook | None = None,
        wait_timeout: float | str | None = None,
        uncertain: tuple[type[BaseException], ...] = (TimeoutError,),
        poll: float = 0.05,
        name: str = "effects",
    ) -> None:
        self.store = _effect_store(store)
        self.ttl = _parse_seconds(ttl)
        self.lease = _parse_seconds(lease)
        if self.ttl <= 0 or self.lease <= 0:
            raise ValueError("ttl and lease must be positive")
        if self.lease > self.ttl:
            raise ValueError("lease must not be longer than ttl")
        if on_unknown is not None and not callable(on_unknown):
            raise TypeError("on_unknown must be callable")
        self.on_unknown = on_unknown
        self.wait_timeout = None if wait_timeout is None else _parse_seconds(wait_timeout)
        self.uncertain = uncertain
        if poll <= 0:
            raise ValueError("poll must be positive")
        self.poll = poll
        self.name = name
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._orphans: set[str] = set()
        self._local_store = isinstance(self.store, MemoryEffectStore)

    # -- keys -----------------------------------------------------------------
    def key(
        self,
        tool: str,
        *,
        call_id: str | None = None,
        arguments: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        business_key: Any = None,
    ) -> str:
        """The ledger key for a call: business key, else call id, else run and digest."""
        return _effect_key(tool, call_id, canonical(arguments), run_id, business_key)

    def lookup(self, key: str) -> EffectRecord | None:
        """The live record for ``key`` (see `key`), or None."""
        return self.store.get(key)

    def forget(self, key: str) -> bool:
        """Delete the record for ``key`` so the next call runs again."""
        with self._lock:
            self._orphans.discard(key)
        return self.store.delete(key)

    # -- helpers --------------------------------------------------------------
    def _new(
        self, key: str, tool: str, digest: str | None, call_id: str | None, attempts: int
    ) -> EffectRecord:
        now = _core.wall_time()
        return EffectRecord(
            key=key,
            tool=tool,
            state="started",
            owner=uuid.uuid4().hex,
            call_id=call_id,
            arguments=digest,
            run_id=run_id_var.get(),
            started_at=now,
            lease_until=now + self.lease,
            attempts=attempts,
        )

    def _is_uncertain(self, exc: BaseException) -> bool:
        if _is_model_retry(exc):
            return False
        return not isinstance(exc, Exception) or isinstance(exc, self.uncertain)

    def _verdict(self, current: EffectRecord, digest: str | None) -> tuple[str, str | None]:
        """What to do about a live record: (action, unknown_reason)."""
        if (
            current.call_id is not None
            and digest is not None
            and current.arguments is not None
            and current.arguments != digest
        ):
            raise EffectConflict(
                f"tool call id {current.call_id!r} was reused with different arguments "
                f"for tool {current.tool!r}",
                record=current,
            )
        if current.state == "succeeded":
            return ("replay", None) if current.has_result else ("unknown", "result_lost")
        if current.state == "failed":
            return ("unknown", "uncertain") if current.uncertain else ("rerun", None)
        with self._lock:
            orphan = current.key in self._orphans and current.key not in self._events
        if orphan or current.lease_until <= _core.wall_time():
            return "unknown", "crashed"
        return "wait", None

    def _unknown(self, current: EffectRecord, reason: str) -> EffectUnknown:
        what = {
            "crashed": "started but never recorded an outcome (crash or store failure)",
            "uncertain": "failed in a way that may have left the effect half done",
            "result_lost": "succeeded but its result could not be stored",
        }[reason]
        _core.emit("tool.effect_unknown", current.tool, run_id=current.run_id, reason=reason)
        return EffectUnknown(
            f"tool {current.tool!r} ({current.key}): a previous attempt {what}; "
            "pass EffectLedger(on_unknown=...) to decide",
            record=current,
            reason=reason,
        )

    def _finished(
        self, owned: EffectRecord, value: Any, exc: BaseException | None
    ) -> list[EffectRecord]:
        """Outcome records to try, in order (the second drops an unstorable result)."""
        now = _core.wall_time()
        if exc is not None:
            return [
                dataclasses.replace(
                    owned,
                    state="failed",
                    finished_at=now,
                    error={"type": type(exc).__name__, "message": str(exc)[:500]},
                    uncertain=self._is_uncertain(exc),
                )
            ]
        done = dataclasses.replace(
            owned, state="succeeded", finished_at=now, result=value, has_result=True
        )
        return [done, dataclasses.replace(done, result=None, has_result=False)]

    def _settle(self, owned: EffectRecord, value: Any, exc: BaseException | None) -> None:
        *first, last = self._finished(owned, value, exc)
        written: bool | None = None
        for record in first:
            try:
                written = self.store.settle(record, self.ttl)
                break
            except (TypeError, ValueError):
                continue  # the result is not serialisable: store the outcome without it
        if written is None:
            written = self.store.settle(last, self.ttl)
        if not written:
            _core.get_logger(None).warning(
                "effect ledger: the record of %s (%s) was taken over after its lease ran "
                "out; this outcome was not stored",
                owned.tool,
                owned.key,
            )

    def _begin_local(self, key: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[key] = event
            self._orphans.discard(key)
        return event

    def _end_local(self, key: str, event: threading.Event, orphaned: bool) -> None:
        with self._lock:
            if self._events.get(key) is event:
                del self._events[key]
            if orphaned:
                self._orphans.add(key)
        event.set()

    def _local_event(self, key: str) -> threading.Event | None:
        with self._lock:
            return self._events.get(key)

    def _decide(self, error: EffectUnknown, answer: Any) -> Any:
        if answer == "retry" or isinstance(answer, EffectResolved):
            return answer
        if answer is None or answer == "raise":
            raise error
        raise TypeError(
            "on_unknown must return 'retry', 'raise', None or EffectResolved(value); "
            f"got {answer!r}"
        )

    # -- sync -----------------------------------------------------------------
    def execute(
        self,
        tool: str,
        func: Callable[..., R],
        args: tuple[Any, ...] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        key: str,
        digest: str | None = None,
        call_id: str | None = None,
    ) -> R:
        """Run ``func(*args, **kwargs)`` at most once per ``key``; see the class docs."""
        kwargs = dict(kwargs or {})
        attempts = 1
        expect: str | None = None
        deadline = None if self.wait_timeout is None else _core.monotonic() + self.wait_timeout
        while True:
            mine = self._new(key, tool, digest, call_id, attempts)
            current = self.store.claim(mine, self.ttl, expect)
            if current is None:
                return self._run_sync(mine, func, args, kwargs)
            expect = None
            action, reason = self._verdict(current, digest)
            if action == "replay":
                return cast(R, current.result)
            if action == "rerun":
                expect, attempts = current.owner, current.attempts + 1
                continue
            if action == "wait":
                self._wait_sync(current, deadline)
                continue
            assert reason is not None
            error = self._unknown(current, reason)
            answer = self.on_unknown(error) if self.on_unknown is not None else None
            if inspect.isawaitable(answer):
                answer = asyncio.run(_await(answer))
            decision = self._decide(error, answer)
            if decision == "retry":
                expect, attempts = current.owner, current.attempts + 1
                continue
            return cast(R, self._resolve_sync(current, decision.value, attempts))

    def _resolve_sync(self, current: EffectRecord, value: Any, attempts: int) -> Any:
        mine = self._new(current.key, current.tool, current.arguments, current.call_id, attempts)
        blocked = self.store.claim(mine, self.ttl, current.owner)
        if blocked is not None:
            raise self._unknown(blocked, "crashed")
        self._settle(mine, value, None)
        with self._lock:
            self._orphans.discard(current.key)
        _core.emit("tool.effect_resolved", current.tool, run_id=current.run_id)
        return value

    def _run_sync(
        self,
        mine: EffectRecord,
        func: Callable[..., R],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R:
        event = self._begin_local(mine.key)
        orphaned = True
        try:
            try:
                value = func(*args, **kwargs)
            except BaseException as exc:
                try:
                    self._settle(mine, None, exc)
                    orphaned = False
                except Exception:
                    _core.get_logger(None).exception(
                        "effect ledger could not record the failure of %s", mine.tool
                    )
                raise
            self._settle(mine, value, None)
            orphaned = False
            return value
        finally:
            self._end_local(mine.key, event, orphaned)

    def _wait_sync(self, current: EffectRecord, deadline: float | None) -> None:
        event = self._local_event(current.key)
        while True:
            now = _core.wall_time()
            pause = max(0.0, current.lease_until - now)
            if deadline is not None:
                left = deadline - _core.monotonic()
                if left <= 0:
                    raise TimeoutExpired(
                        f"tool {current.tool!r} is still running elsewhere ({current.key})",
                        seconds=self.wait_timeout or 0.0,
                    )
                pause = min(pause, left)
            if event is not None:
                event.wait(min(pause, 1.0) if pause else 0)
                if event.is_set():
                    return
            else:
                _core.sleep(min(self.poll, pause) if pause else 0)
            latest = self.store.get(current.key)
            if latest is None or latest.owner != current.owner or latest.state != "started":
                return
            if latest.lease_until <= _core.wall_time() or self._orphaned(current.key):
                return

    def _orphaned(self, key: str) -> bool:
        with self._lock:
            return key in self._orphans

    # -- async ----------------------------------------------------------------
    async def _store(self, method: Callable[..., Any], *args: Any) -> Any:
        if self._local_store:
            return method(*args)
        return await asyncio.to_thread(method, *args)

    async def aexecute(
        self,
        tool: str,
        func: Callable[..., Awaitable[R]],
        args: tuple[Any, ...] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        key: str,
        digest: str | None = None,
        call_id: str | None = None,
    ) -> R:
        """The async form of `execute` for an ``async def`` ``func``."""
        kwargs = dict(kwargs or {})
        attempts = 1
        expect: str | None = None
        deadline = None if self.wait_timeout is None else _core.monotonic() + self.wait_timeout
        while True:
            mine = self._new(key, tool, digest, call_id, attempts)
            current = await self._store(self.store.claim, mine, self.ttl, expect)
            if current is None:
                return await self._run_async(mine, func, args, kwargs)
            expect = None
            action, reason = self._verdict(current, digest)
            if action == "replay":
                return cast(R, current.result)
            if action == "rerun":
                expect, attempts = current.owner, current.attempts + 1
                continue
            if action == "wait":
                await self._wait_async(current, deadline)
                continue
            assert reason is not None
            error = self._unknown(current, reason)
            answer = self.on_unknown(error) if self.on_unknown is not None else None
            if inspect.isawaitable(answer):
                answer = await answer
            decision = self._decide(error, answer)
            if decision == "retry":
                expect, attempts = current.owner, current.attempts + 1
                continue
            value = decision.value
            mine = self._new(key, tool, current.arguments, current.call_id, attempts)
            blocked = await self._store(self.store.claim, mine, self.ttl, current.owner)
            if blocked is not None:
                raise self._unknown(blocked, "crashed")
            await self._store(self._settle, mine, value, None)
            with self._lock:
                self._orphans.discard(key)
            _core.emit("tool.effect_resolved", tool, run_id=current.run_id)
            return cast(R, value)

    async def _run_async(
        self,
        mine: EffectRecord,
        func: Callable[..., Awaitable[R]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> R:
        event = self._begin_local(mine.key)
        orphaned = True
        try:
            try:
                value = await func(*args, **kwargs)
            except BaseException as exc:
                try:
                    await self._store(self._settle, mine, None, exc)
                    orphaned = False
                except Exception:
                    _core.get_logger(None).exception(
                        "effect ledger could not record the failure of %s", mine.tool
                    )
                raise
            await self._store(self._settle, mine, value, None)
            orphaned = False
            return value
        finally:
            self._end_local(mine.key, event, orphaned)

    async def _wait_async(self, current: EffectRecord, deadline: float | None) -> None:
        while True:
            pause = max(0.0, current.lease_until - _core.wall_time())
            if deadline is not None:
                left = deadline - _core.monotonic()
                if left <= 0:
                    raise TimeoutExpired(
                        f"tool {current.tool!r} is still running elsewhere ({current.key})",
                        seconds=self.wait_timeout or 0.0,
                    )
                pause = min(pause, left)
            event = self._local_event(current.key)
            await asyncio.sleep(min(self.poll if event is None else 0.005, pause))
            if event is not None and event.is_set():
                return
            latest = await self._store(self.store.get, current.key)
            if latest is None or latest.owner != current.owner or latest.state != "started":
                return
            if latest.lease_until <= _core.wall_time() or self._orphaned(current.key):
                return

    def __repr__(self) -> str:
        return f"EffectLedger({type(self.store).__name__}, lease={self.lease:g}s)"


def _effect_key(
    tool: str, call_id: str | None, digest: str | None, run_id: str | None, business_key: Any
) -> str:
    if business_key is not None:
        return f"{tool}:key:{business_key}"
    if call_id is not None:
        return f"{tool}:call:{call_id}"
    return f"{tool}:{run_id or '-'}:{digest}"


def _is_model_retry(exc: BaseException) -> bool:
    """True for pydantic-ai's ``ModelRetry`` (matched by class name, never imported)."""
    return any(cls.__name__ == "ModelRetry" for cls in type(exc).__mro__)


# --------------------------------------------------------------------------- tools


@dataclass
class _CallState:
    ran: bool = False


_call_state: contextvars.ContextVar[_CallState | None] = contextvars.ContextVar(
    "python_wrap_tool_call_state", default=None
)
_call_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "python_wrap_tool_call_key", default=""
)


@dataclass
class _Decision:
    dry_run: bool = False
    approver: Approver | None = None
    approval_timeout: float = 60.0
    policies: list[ToolPolicy] = field(default_factory=list)


def _policies() -> list[ToolPolicy]:
    """Policies in effect here, innermost first."""
    found: list[ToolPolicy] = []
    run = _run_var.get()
    while run is not None:
        for policy in (run.guard.policy, run.guard._extra_policy):
            if policy is not None:
                found.append(policy)
        run = run.parent
    extra = _registry_policy.get()
    if extra is not None:
        found.append(extra)
    return found


def _deny(error: type[ToolNotAllowed], spec: ToolSpec, reason: str, message: str) -> ToolNotAllowed:
    run_id = run_id_var.get()
    _core.emit("tool.denied", spec.name, run_id=run_id, reason=reason)
    return error(message, tool=spec.name, reason=reason, run_id=run_id)


def _authorize(spec: ToolSpec) -> _Decision:
    decision = _Decision(policies=_policies())
    for policy in decision.policies:
        if not policy.permits(spec.name):
            raise _deny(ToolNotAllowed, spec, "denied", f"tool {spec.name!r} is not allowed here")
        if not policy.allow_write and not spec.read_only:
            raise _deny(
                ToolNotAllowed,
                spec,
                "read_only",
                f"tool {spec.name!r} has side effects and this run is read-only "
                "(allow_write=False); declare it read_only=True if it has none",
            )
        if policy.dry_run and not spec.read_only:
            decision.dry_run = True
        if decision.approver is None and policy.approver is not None:
            decision.approver = policy.approver
            decision.approval_timeout = policy.approval_timeout
    return decision


def _approval_result(spec: ToolSpec, ok: Any, error: BaseException | None) -> None:
    if error is not None:
        raise _deny(
            ApprovalDenied, spec, "approval_denied", f"approver failed for tool {spec.name!r}"
        ) from error
    if ok is not True:
        raise _deny(
            ApprovalDenied, spec, "approval_denied", f"approval for tool {spec.name!r} was denied"
        )


def _no_approver(spec: ToolSpec) -> ToolNotAllowed:
    return _deny(
        ApprovalDenied,
        spec,
        "approval_denied",
        f"tool {spec.name!r} requires approval but no approver is configured "
        "(set ToolPolicy(approver=...))",
    )


def _timed_out(spec: ToolSpec, seconds: float) -> ToolNotAllowed:
    return _deny(
        ApprovalTimeout,
        spec,
        "approval_timeout",
        f"no approval for tool {spec.name!r} within {seconds:g}s",
    )


def _approve_sync(spec: ToolSpec, decision: _Decision, call: ToolCall) -> None:
    approver = decision.approver
    if approver is None:
        raise _no_approver(spec)
    box: dict[str, Any] = {}
    done = threading.Event()

    def ask() -> None:
        try:
            answer = approver(call)
            if inspect.isawaitable(answer):
                answer = asyncio.run(_await(answer))
            box["ok"] = answer
        except BaseException as error:
            box["error"] = error
        finally:
            done.set()

    context = contextvars.copy_context()
    threading.Thread(
        target=context.run, args=(ask,), daemon=True, name="python-wrap-approval"
    ).start()
    if not done.wait(decision.approval_timeout):
        raise _timed_out(spec, decision.approval_timeout)
    _approval_result(spec, box.get("ok"), box.get("error"))


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


async def _approve_async(spec: ToolSpec, decision: _Decision, call: ToolCall) -> None:
    approver = decision.approver
    if approver is None:
        raise _no_approver(spec)
    limit = decision.approval_timeout

    async def ask() -> Any:
        if _core.is_async(approver):
            return await cast(Callable[[ToolCall], Awaitable[Any]], approver)(call)
        answer = await asyncio.to_thread(approver, call)
        if inspect.isawaitable(answer):
            answer = await answer
        return answer

    ok: Any = None
    error: BaseException | None = None
    try:
        ok = await asyncio.wait_for(ask(), limit)
    except asyncio.TimeoutError:
        raise _timed_out(spec, limit) from None
    except Exception as exc:
        error = exc
    _approval_result(spec, ok, error)


def _dry(
    spec: ToolSpec, dry_result: Any, arguments: Mapping[str, Any], args: Any, kwargs: Any
) -> Any:
    _core.emit("tool.dry_run", spec.name, run_id=run_id_var.get())
    if dry_result is _MISSING:
        return DryRun(spec.name, arguments)
    if callable(dry_result):
        return dry_result(*args, **kwargs)
    return dry_result


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


_MISSING: Any = _Missing()


def _description(func: Callable[..., Any], given: str | None) -> str:
    if given is not None:
        return given
    doc = inspect.getdoc(func) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


@overload
def tool(func: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def tool(
    func: None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = False,
    idempotent_key: bool | Callable[..., object] | None = None,
    requires_approval: bool = False,
    timeout: float | str | None = None,
    retry: Callable[[Any], Any] | None = None,
    dry_result: Any = _MISSING,
    store: Any = None,
    ttl: float | str = "1d",
    ledger: EffectLedger | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(
    func: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = False,
    idempotent_key: bool | Callable[..., object] | None = None,
    requires_approval: bool = False,
    timeout: float | str | None = None,
    retry: Callable[[Any], Any] | None = None,
    dry_result: Any = _MISSING,
    store: Any = None,
    ttl: float | str = "1d",
    ledger: EffectLedger | None = None,
) -> Any:
    """Mark a sync or async function as an agent tool with replay-safe side effects.

    Every call, in this order: counts as a ``"tool"`` step of the current run (limits and
    loop detection), passes the `ToolPolicy` checks (allow/deny, read-only), returns the
    dry-run result in a dry run, asks the approver when ``requires_approval=True``, then
    runs, deduplicated by its idempotency key, under ``retry`` and ``timeout``.

    Idempotency: a tool with side effects (``read_only=False``) gets a key from the tool
    name, the canonical arguments and the current run id. A second call with the same key
    within ``ttl`` (a retried step, a replayed run with the same ``run_id``, a model that
    asks twice) returns the stored result without running, and emits ``tool.replayed``.
    Only successful results are stored; a failing call runs again next time. A concurrent
    duplicate waits for the first call and returns its result.

    Args:
        func: The tool function (for bare ``@tool``).
        name: Name the model uses; the function name by default.
        description: First docstring line by default.
        read_only: True for tools without side effects: they run in read-only and dry
            runs and are not deduplicated by default.
        idempotent_key: None derives a key for write tools only; True derives one for
            read-only tools too; False disables deduplication; a callable receives the
            tool's arguments and returns a key (used as is, prefixed by the tool name,
            not scoped to the run: use it for business keys such as an order id).
        requires_approval: Ask the run's approver before every call; denied without one.
        timeout: Per-call time limit (seconds or ``"30s"``), applied inside ``retry``.
        retry: A decorator such as ``retry(max_attempts=3, on=ConnectionError)``,
            applied inside the deduplication, so a replay never re-runs it.
        dry_result: Returned in a dry run instead of running (a callable is called with
            the tool's arguments); a `DryRun` marker by default.
        store: Result store for deduplication (shared across processes with
            ``RedisStore(fail_open=False)``); in-process `MemoryResultStore` by default.
        ttl: How long a result is replayed (``"1d"`` by default).
        ledger: An `EffectLedger` to deduplicate with instead of ``store``: the key is
            the provider's tool-call id when one is current (`with_call_id`; adapters
            set it), so a replayed call returns the stored result while a new call from
            the model runs; without an id it is the run id and argument digest as above.
            Calls are recorded as started / succeeded / failed, so a crash between the
            side effect and the record is detected (`EffectUnknown`). A ledger turns
            deduplication on for read-only tools too, unless ``idempotent_key=False``.

    Returns:
        The wrapped function, with the same signature. `tool_spec` reads its `ToolSpec`.

    Example:
        ```python
        from python_wrap import agent_run, tool

        sent = []

        @tool
        def send_email(to: str, subject: str) -> str:
            sent.append(to)
            return f"queued for {to}"

        with agent_run(run_id="run-1"):
            assert send_email("ada@example.com", "hi") == "queued for ada@example.com"
            send_email("ada@example.com", "hi")  # replayed, not sent again
        assert sent == ["ada@example.com"]
        ```
    """
    if func is None:
        return functools.partial(
            tool,
            name=name,
            description=description,
            read_only=read_only,
            idempotent_key=idempotent_key,
            requires_approval=requires_approval,
            timeout=timeout,
            retry=retry,
            dry_result=dry_result,
            store=store,
            ttl=ttl,
            ledger=ledger,
        )
    if not callable(func):
        raise TypeError("tool() expects a function")
    if not (idempotent_key is None or isinstance(idempotent_key, bool) or callable(idempotent_key)):
        raise TypeError("idempotent_key must be None, a bool or a callable")
    limit = None if timeout is None else _parse_seconds(timeout)
    if ledger is not None and not isinstance(ledger, EffectLedger):
        raise TypeError("ledger must be an EffectLedger")
    dedupe = idempotent_key is not False and (
        idempotent_key is not None or not read_only or ledger is not None
    )
    is_async = _core.is_async(func)
    spec = ToolSpec(
        name=name or func.__name__,
        description=_description(func, description),
        read_only=bool(read_only),
        idempotent=dedupe,
        requires_approval=bool(requires_approval),
        timeout=limit,
        retried=retry is not None,
        is_async=is_async,
    )
    signature = inspect.signature(func)
    params = list(signature.parameters)
    skip_first = bool(params) and (
        params[0] in ("self", "cls") or _is_framework_context(signature.parameters[params[0]])
    )

    core: Callable[..., Any] = func
    if limit is not None:
        from python_wrap.concurrency import timeout as _timeout

        core = _timeout(limit)(core)
    if retry is not None:
        core = retry(core)

    def bind(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        if skip_first:
            arguments.pop(params[0], None)
        return arguments

    def make_key(args: tuple[Any, ...], kwargs: dict[str, Any], digest: str | None) -> str:
        if callable(idempotent_key) and not isinstance(idempotent_key, bool):
            return f"tool:{spec.name}:key:{idempotent_key(*args, **kwargs)}"
        return f"tool:{spec.name}:{run_id_var.get() or '-'}:{digest}"

    def ledger_key(args: tuple[Any, ...], kwargs: dict[str, Any], digest: str | None) -> str:
        business = None
        if callable(idempotent_key) and not isinstance(idempotent_key, bool):
            business = idempotent_key(*args, **kwargs)
        return _effect_key(spec.name, tool_call_id.get(), digest, run_id_var.get(), business)

    runner: Callable[..., Any] = core
    if dedupe and ledger is not None:
        if is_async:

            async def ledger_async(*args: Any, **kwargs: Any) -> Any:
                _mark_ran(spec)
                return await core(*args, **kwargs)

            async def run_ledger_async(*args: Any, **kwargs: Any) -> Any:
                digest = _call_key.get()
                return await ledger.aexecute(
                    spec.name,
                    ledger_async,
                    args,
                    kwargs,
                    key=ledger_key(args, kwargs, digest),
                    digest=digest,
                    call_id=tool_call_id.get(),
                )

            runner = run_ledger_async
        else:

            def ledger_sync(*args: Any, **kwargs: Any) -> Any:
                _mark_ran(spec)
                return core(*args, **kwargs)

            def run_ledger(*args: Any, **kwargs: Any) -> Any:
                digest = _call_key.get()
                return ledger.execute(
                    spec.name,
                    ledger_sync,
                    args,
                    kwargs,
                    key=ledger_key(args, kwargs, digest),
                    digest=digest,
                    call_id=tool_call_id.get(),
                )

            runner = run_ledger
    elif dedupe:
        from python_wrap.idempotency import idempotent

        def key_of(*args: Any, **kwargs: Any) -> str:
            return _call_key.get()

        if is_async:

            async def marked_async(*args: Any, **kwargs: Any) -> Any:
                _mark_ran(spec)
                return await core(*args, **kwargs)

            runner = idempotent(key=key_of, ttl=ttl, store=store, in_progress="wait")(marked_async)
        else:

            def marked(*args: Any, **kwargs: Any) -> Any:
                _mark_ran(spec)
                return core(*args, **kwargs)

            runner = idempotent(key=key_of, ttl=ttl, store=store, in_progress="wait")(marked)

    def call_key(args: tuple[Any, ...], kwargs: dict[str, Any], digest: str | None) -> str:
        # With a ledger the context carries the digest; the ledger builds its own key.
        return (digest or "") if ledger is not None else make_key(args, kwargs, digest)

    def prepare(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[_Decision, ToolCall, str | None]:
        arguments = bind(args, kwargs)
        digest = canonical(arguments)
        run = _run_var.get()
        if run is not None:
            run._record("tool", spec.name, digest)
        decision = _authorize(spec)
        call = ToolCall(
            spec.name,
            MappingProxyType(arguments),
            run_id_var.get(),
            spec.read_only,
            spec.description,
            tool_call_id.get(),
        )
        return decision, call, digest

    if is_async:

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            decision, call, digest = prepare(args, kwargs)
            if decision.dry_run:
                return _dry(spec, dry_result, call.arguments, args, kwargs)
            if spec.requires_approval:
                await _approve_async(spec, decision, call)
            if not dedupe:
                _core.emit("tool.call", spec.name, run_id=call.run_id)
                return await runner(*args, **kwargs)
            state = _CallState()
            resets = (_call_state.set(state), _call_key.set(call_key(args, kwargs, digest)))
            try:
                result = await runner(*args, **kwargs)
            finally:
                _call_key.reset(resets[1])
                _call_state.reset(resets[0])
            if not state.ran:
                _core.emit("tool.replayed", spec.name, run_id=call.run_id)
            return result

        wrapper: Callable[..., Any] = async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            decision, call, digest = prepare(args, kwargs)
            if decision.dry_run:
                return _dry(spec, dry_result, call.arguments, args, kwargs)
            if spec.requires_approval:
                _approve_sync(spec, decision, call)
            if not dedupe:
                _core.emit("tool.call", spec.name, run_id=call.run_id)
                return runner(*args, **kwargs)
            state = _CallState()
            resets = (_call_state.set(state), _call_key.set(call_key(args, kwargs, digest)))
            try:
                result = runner(*args, **kwargs)
            finally:
                _call_key.reset(resets[1])
                _call_state.reset(resets[0])
            if not state.ran:
                _core.emit("tool.replayed", spec.name, run_id=call.run_id)
            return result

        wrapper = sync_wrapper

    # Let `doctor` see the retry/timeout layers inside the tool.
    wrapper.__wrapped__ = core  # type: ignore[attr-defined]
    setattr(wrapper, _SPEC_ATTR, spec)
    setattr(
        wrapper,
        _LAYER_ATTR,
        (
            "tool",
            {
                "name": spec.name,
                "read_only": spec.read_only,
                "idempotent": spec.idempotent,
                "requires_approval": spec.requires_approval,
            },
        ),
    )
    return wrapper


_CONTEXT_TYPES = ("RunContext", "ToolContext", "RunContextWrapper")


def _is_framework_context(param: inspect.Parameter) -> bool:
    """True for a first parameter that is an agent framework's run context.

    pydantic-ai passes ``ctx: RunContext[...]`` and the OpenAI Agents SDK
    ``ctx: RunContextWrapper[...]`` / ``ToolContext``; they differ on every call, so they
    are left out of the argument digest (like ``self``).
    """
    annotation = param.annotation
    if annotation is inspect.Parameter.empty:
        return False
    text = annotation if isinstance(annotation, str) else repr(annotation)
    origin = getattr(annotation, "__origin__", annotation)
    name = getattr(origin, "__name__", "") if not isinstance(annotation, str) else ""
    return name in _CONTEXT_TYPES or any(
        text == kind or text.startswith(kind + "[") or f".{kind}" in text or f"{kind}[" in text
        for kind in _CONTEXT_TYPES
    )


def _mark_ran(spec: ToolSpec) -> None:
    state = _call_state.get()
    if state is not None:
        state.ran = True
    _core.emit("tool.call", spec.name, run_id=run_id_var.get())


def tool_spec(func: Callable[..., Any]) -> ToolSpec | None:
    """The `ToolSpec` of a function decorated with `tool` (bound methods too), or None."""
    spec = getattr(getattr(func, "__func__", func), _SPEC_ATTR, None)
    return spec if isinstance(spec, ToolSpec) else None


# --------------------------------------------------------------------------- registry


class ToolRegistry:
    """Tools by name, dispatched the way a model asks for them.

    Args:
        tools: Functions to register (plain functions are wrapped with `tool` defaults,
            which treats them as having side effects).
        policy: A `ToolPolicy` applied to every `call`, and to every tool call inside
            `run`.
        guard: The `RunGuard` that `run` uses; ``doctor`` reports PW017 when it is
            missing or sets no limits.

    Example:
        ```python
        from python_wrap import RunGuard, ToolNotAllowed, ToolPolicy, ToolRegistry

        tools = ToolRegistry(policy=ToolPolicy(deny=["drop_*"]), guard=RunGuard(max_steps=8))

        @tools.tool(read_only=True)
        def lookup(sku: str) -> int:
            return 3

        @tools.tool
        def drop_table(name: str) -> None: ...

        with tools.run():
            assert tools.call("lookup", '{"sku": "A-1"}') == 3
            try:
                tools.call("drop_table", {"name": "orders"})
            except ToolNotAllowed as error:
                assert error.reason == "denied"
        ```
    """

    def __init__(
        self,
        tools: Iterable[Callable[..., Any]] = (),
        *,
        policy: ToolPolicy | None = None,
        guard: RunGuard | None = None,
    ) -> None:
        if policy is not None and not isinstance(policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        if guard is not None and not isinstance(guard, RunGuard):
            raise TypeError("guard must be a RunGuard")
        self.policy = policy
        self.guard = guard
        self._tools: dict[str, Callable[..., Any]] = {}
        self._lock = threading.Lock()
        for func in tools:
            self.add(func)

    def add(self, func: Callable[P, R]) -> Callable[P, R]:
        """Register ``func`` (wrapping it with `tool` first if needed) and return it."""
        wrapped: Callable[..., Any] = func if tool_spec(func) is not None else tool(func)
        spec = tool_spec(wrapped)
        assert spec is not None
        with self._lock:
            if spec.name in self._tools:
                raise ValueError(f"a tool named {spec.name!r} is already registered")
            self._tools[spec.name] = wrapped
        return cast(Callable[P, R], wrapped)

    @overload
    def tool(self, func: Callable[P, R], /) -> Callable[P, R]: ...

    @overload
    def tool(
        self, func: None = None, /, **options: Any
    ) -> Callable[[Callable[P, R]], Callable[P, R]]: ...

    def tool(self, func: Callable[..., Any] | None = None, /, **options: Any) -> Any:
        """`tool` that also registers the function: ``@registry.tool(read_only=True)``."""
        if func is None:
            return lambda f: self.add(tool(**options)(f))
        return self.add(tool(func))

    def get(self, name: str) -> Callable[..., Any]:
        """The registered tool called ``name``.

        Raises:
            ToolNotAllowed: No such tool (reason ``"unknown"``).
        """
        with self._lock:
            found = self._tools.get(name)
        if found is None:
            run_id = run_id_var.get()
            _core.emit("tool.denied", name, run_id=run_id, reason="unknown")
            raise ToolNotAllowed(
                f"unknown tool {name!r}", tool=name, reason="unknown", run_id=run_id
            )
        return found

    def specs(self) -> list[ToolSpec]:
        """The `ToolSpec` of every registered tool, in registration order."""
        with self._lock:
            funcs = list(self._tools.values())
        return [spec for spec in map(tool_spec, funcs) if spec is not None]

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._tools

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._tools))

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def call(self, name: str, arguments: Mapping[str, Any] | str | None = None) -> Any:
        """Call the tool ``name`` with keyword ``arguments`` (a mapping or a JSON object).

        For an ``async def`` tool this returns a coroutine to await.

        Raises:
            ToolNotAllowed: Unknown tool or refused by a policy.
            ValueError: ``arguments`` is not a JSON object.
        """
        func = self.get(name)
        if arguments is None:
            kwargs: dict[str, Any] = {}
        else:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(parsed, Mapping):
                raise ValueError(f"arguments for tool {name!r} must be a JSON object")
            kwargs = dict(parsed)
        spec = tool_spec(func)
        if spec is not None and spec.is_async:
            return self._call_async(func, kwargs)
        reset = _registry_policy.set(self.policy)
        try:
            return func(**kwargs)
        finally:
            _registry_policy.reset(reset)

    async def _call_async(self, func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        reset = _registry_policy.set(self.policy)
        try:
            return await func(**kwargs)
        finally:
            _registry_policy.reset(reset)

    def run(self, *, run_id: str | None = None) -> RunGuard:
        """A run of ``guard`` (limitless when None) with this registry's policy in effect."""
        base = self.guard or RunGuard()
        if run_id is not None:
            base = RunGuard(
                max_steps=base.max_steps,
                max_seconds=base.max_seconds,
                max_repeats=base.max_repeats,
                budget=base.budget,
                policy=base.policy,
                run_id=run_id,
                name=base.name,
            )
        return base._with_policy(self.policy)

    def _explain(self, *, idempotent: bool = True, multiprocess: bool = False) -> Any:
        from python_wrap.doctor import Finding, Layer, Report, _check, doctor

        guard_layer = Layer(
            "agent_run",
            (self.guard or RunGuard()).settings(),
        )
        layers: list[Layer] = [guard_layer]
        findings: list[Finding] = list(_check([guard_layer], idempotent, multiprocess))
        with self._lock:
            funcs = list(self._tools.values())
        for func in funcs:
            report = doctor(func, idempotent=idempotent, multiprocess=multiprocess)
            layers += report.layers
            findings += [item for item in report.findings if item not in findings]
        findings.sort(key=lambda item: (item.severity != "error", item.code))
        return Report(f"ToolRegistry({len(funcs)} tools)", tuple(layers), tuple(findings))

    def __repr__(self) -> str:
        return f"ToolRegistry({sorted(self)!r})"
