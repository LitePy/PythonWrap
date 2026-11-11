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

Everything publishes events (``agent.run.*``, ``tool.*``) through `python_wrap.events`.
"""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import functools
import hashlib
import inspect
import json
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Any, ParamSpec, TypeVar, cast, overload

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
    "LoopDetected",
    "RunBudgetExceeded",
    "RunCancelled",
    "RunGuard",
    "RunTimeout",
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
    "tool_spec",
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
        if budget is not None:
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
        )
    if not callable(func):
        raise TypeError("tool() expects a function")
    if not (idempotent_key is None or isinstance(idempotent_key, bool) or callable(idempotent_key)):
        raise TypeError("idempotent_key must be None, a bool or a callable")
    limit = None if timeout is None else _parse_seconds(timeout)
    dedupe = idempotent_key is not False and (idempotent_key is not None or not read_only)
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
    skip_first = bool(params) and params[0] in ("self", "cls")

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

    runner: Callable[..., Any] = core
    if dedupe:
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
            resets = (_call_state.set(state), _call_key.set(make_key(args, kwargs, digest)))
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
            resets = (_call_state.set(state), _call_key.set(make_key(args, kwargs, digest)))
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
