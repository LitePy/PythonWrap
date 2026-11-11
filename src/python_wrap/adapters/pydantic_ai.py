"""python-wrap for pydantic-ai agents: guarded tools, one budget, an audit tape, loop guards.

pydantic-ai is never imported at module level. `PythonWrapCapability` holds the settings
and does the work; it reaches pydantic-ai in two duck-typed ways:

* `PythonWrapCapability.wrap` turns any pydantic-ai toolset into a ``WrapperToolset``
  (imported lazily) whose ``call_tool`` goes through the capability, and
  `PythonWrapCapability.toolset` builds one from plain functions;
* `PythonWrapCapability.tool` (or `guard_tool`) wraps a single tool function for
  ``agent.tool`` / ``agent.tool_plain`` and keeps its signature and docstring, so the
  schema pydantic-ai builds is unchanged.

Every tool call then: counts against ``max_tool_calls`` / ``max_repeats`` and the current
`RunGuard` (identical repeated calls end the run with `LoopDetected`), passes the
`ToolPolicy` checks and approval, is deduplicated by an `EffectLedger` keyed on
pydantic-ai's ``tool_call_id``, runs under a per-attempt ``timeout`` and ``retry`` that
never retries ``ModelRetry`` (that exception is pydantic-ai's way of asking the model to
try again, so it passes straight through), and is written to an `AuditTape`.

Token usage: ``RunContext.usage`` (seen at every tool call) and the run result's
``usage()`` are running totals; `PythonWrapCapability` adds only their growth to a
python-wrap `Budget`, so one budget can span pydantic-ai runs, direct SDK calls and
other frameworks. Keep pydantic-ai's ``UsageLimits`` for per-run request and token caps
enforced inside the model loop; use the `Budget` for dollars, per-user or per-tenant
limits, limits across many runs and processes, and stacks that mix frameworks.
"""

from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import functools
import importlib
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any, TypeVar, cast

from python_wrap import _core
from python_wrap.adapters._frameworks import (
    _LAYER_ATTR,
    CallCounter,
    PassThrough,
    UsageBridge,
    is_model_retry,
    record_llm,
    record_tool,
    usage_totals,
)
from python_wrap.agents import (
    EffectLedger,
    RunGuard,
    ToolNotAllowed,
    ToolPolicy,
    current_run,
    tool,
    with_call_id,
)

__all__ = ["PythonWrapCapability", "guard_tool", "usage_totals"]

F = TypeVar("F", bound=Callable[..., Any])
FRAMEWORK = "pydantic-ai"

_NEXT: contextvars.ContextVar[Callable[[], Awaitable[Any]] | None] = contextvars.ContextVar(
    "python_wrap_pydantic_ai_next", default=None
)


def _matcher(patterns: Iterable[str] | str | None) -> Callable[[str], bool]:
    if patterns is None:
        return lambda name: False
    found = (patterns,) if isinstance(patterns, str) else tuple(patterns)
    return lambda name: any(fnmatch.fnmatchcase(name, p) for p in found)


def _passthrough(func: Callable[..., Any]) -> Callable[..., Any]:
    """Turn ``ModelRetry`` into `PassThrough` so an enclosing retry cannot catch it."""
    if _core.is_async(func):

        @functools.wraps(func)
        async def async_inner(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                if is_model_retry(exc):
                    raise PassThrough(exc) from None
                raise

        return async_inner

    @functools.wraps(func)
    def inner(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if is_model_retry(exc):
                raise PassThrough(exc) from None
            raise

    return inner


def _unwrap(func: Callable[..., Any]) -> Callable[..., Any]:
    if _core.is_async(func):

        @functools.wraps(func)
        async def async_outer(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except PassThrough as wrapped:
                raise wrapped.error from None

        return async_outer

    @functools.wraps(func)
    def outer(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except PassThrough as wrapped:
            raise wrapped.error from None

    return outer


def compose(
    func: Callable[..., Any],
    *,
    timeout: float | str | None,
    retry: Callable[[Callable[..., Any]], Callable[..., Any]] | None,
) -> Callable[..., Any]:
    """``func`` under a per-attempt timeout and a retry that lets ``ModelRetry`` through."""
    core = func
    if timeout is not None:
        from python_wrap._runtime import seconds
        from python_wrap.concurrency import timeout as make_timeout

        core = make_timeout(seconds(timeout))(core)
    if retry is None:
        return core
    return _unwrap(retry(_passthrough(core)))


class PythonWrapCapability:
    """python-wrap's tool safety, budget, audit and loop guards for pydantic-ai agents.

    Args:
        timeout: Per-attempt time limit for every tool (seconds or ``"30s"``); None
            leaves tools unbounded (`doctor` reports PW020 for wrapped tools).
        retry: A retry decorator (``retry(max_attempts=3, on=ConnectionError)``) applied
            per call, inside the ledger; ``ModelRetry`` is never retried.
        ledger: An `EffectLedger`; side-effect tools are then replay-safe per
            ``tool_call_id``. Without one tools are not deduplicated.
        read_only: Glob patterns of tools without side effects (they still run in
            read-only and dry runs of a `ToolPolicy`).
        approval: Glob patterns of tools that need the approver's yes (``ToolPolicy``
            of the current run, or ``policy``).
        policy: A `ToolPolicy` in effect for every call (in addition to the run's).
        guard: A `RunGuard` entered by `run` / `run_sync`.
        budget: A python-wrap `Budget` fed with the run's token usage.
        tape: An `AuditTape` receiving every tool call and the run's usage.
        max_tool_calls: Most tool calls per run (`StepLimitExceeded`).
        max_repeats: Most identical tool calls (same name and arguments) per run
            (`LoopDetected`).
        on_denied: ``"raise"`` ends the run when a policy or the approver refuses a
            tool; ``"retry"`` tells the model instead (as ``ModelRetry``).
        model: The model name used to price usage that does not name one.

    Example:
        ```python
        from python_wrap import EffectLedger
        from python_wrap.adapters.pydantic_ai import PythonWrapCapability

        cap = PythonWrapCapability(timeout=10, ledger=EffectLedger(), max_repeats=2)

        @cap.tool
        def add_note(ticket: str, text: str) -> str:
            "Attach a note to a ticket."
            return f"noted {ticket}"

        assert add_note("T-1", "hi") == "noted T-1"
        assert add_note.__doc__ == "Attach a note to a ticket."
        ```
    """

    def __init__(
        self,
        *,
        timeout: float | str | None = None,
        retry: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
        ledger: EffectLedger | None = None,
        read_only: Iterable[str] | str | None = None,
        approval: Iterable[str] | str | None = None,
        policy: ToolPolicy | None = None,
        guard: RunGuard | None = None,
        budget: Any = None,
        tape: Any = None,
        max_tool_calls: int | None = None,
        max_repeats: int | None = None,
        on_denied: str = "raise",
        model: str | None = None,
    ) -> None:
        if on_denied not in ("raise", "retry"):
            raise ValueError("on_denied must be 'raise' or 'retry'")
        if ledger is not None and not isinstance(ledger, EffectLedger):
            raise TypeError("ledger must be an EffectLedger")
        if policy is not None and not isinstance(policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        if guard is not None and not isinstance(guard, RunGuard):
            raise TypeError("guard must be a RunGuard")
        self.timeout = timeout
        self.retry = retry
        self.ledger = ledger
        self.policy = policy
        self.guard = guard
        self.budget = budget
        self.tape = tape
        self.on_denied = on_denied
        self.model = model
        self.counter = CallCounter(max_tool_calls, max_repeats)
        self.bridge = UsageBridge(budget, model)
        self._read_only = _matcher(read_only)
        self._approval = _matcher(approval)
        self._tools: dict[str, Callable[..., Any]] = {}

    # -- tool functions ---------------------------------------------------------
    def _python_wrap_tool(self, func: Callable[..., Any], name: str) -> Callable[..., Any]:
        decorate = tool(
            name=name,
            read_only=self._read_only(name),
            requires_approval=self._approval(name),
            ledger=self.ledger,
            idempotent_key=None if self.ledger is not None else False,
        )
        return decorate(compose(func, timeout=self.timeout, retry=self.retry))

    def tool(self, func: F | None = None, /, *, name: str | None = None) -> Any:
        """Wrap one tool function for ``agent.tool`` / ``agent.tool_plain``.

        The signature, annotations and docstring are kept, so pydantic-ai builds the
        same schema. A first ``ctx: RunContext`` parameter supplies the
        ``tool_call_id`` and the running usage; it is left out of the argument digest.
        """
        if func is None:
            return functools.partial(self.tool, name=name)
        label = name or func.__name__
        guarded = self._python_wrap_tool(func, label)

        def admit(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, str | None]:
            ctx = args[0] if args and hasattr(args[0], "tool_call_id") else None
            shown = dict(kwargs) or list(args[1 if ctx else 0 :])
            self.counter.admit(label, shown)
            if ctx is not None:
                self.bridge.sync(getattr(ctx, "usage", None))
            self.bridge.check(label)
            return shown, None if ctx is None else getattr(ctx, "tool_call_id", None)

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                shown, call_id = admit(args, kwargs)
                started = _core.monotonic()
                try:
                    with with_call_id(call_id or None):
                        result = await self._denied_as_retry(guarded(*args, **kwargs))
                except BaseException as exc:
                    self._record(label, shown, None, exc, started, call_id)
                    raise
                self._record(label, shown, result, None, started, call_id)
                return result

            wrapper: Callable[..., Any] = async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                shown, call_id = admit(args, kwargs)
                started = _core.monotonic()
                try:
                    with with_call_id(call_id or None), self._denials():
                        result = guarded(*args, **kwargs)
                except BaseException as exc:
                    self._record(label, shown, None, exc, started, call_id)
                    raise
                self._record(label, shown, result, None, started, call_id)
                return result

            wrapper = sync_wrapper
        setattr(wrapper, _LAYER_ATTR, self._layer(label))
        return wrapper

    def _layer(self, name: str) -> tuple[str, dict[str, Any]]:
        return (
            "framework_tool",
            {"framework": FRAMEWORK, "name": name, "timeout": self.timeout},
        )

    def _record(
        self,
        name: str,
        arguments: Any,
        result: Any,
        error: BaseException | None,
        started: float,
        call_id: str | None,
    ) -> None:
        record_tool(
            self.tape,
            framework=FRAMEWORK,
            name=name,
            arguments=arguments,
            result=result,
            error=error,
            started=started,
            call_id=call_id,
        )

    @contextlib.contextmanager
    def _denials(self) -> Iterator[None]:
        try:
            yield
        except ToolNotAllowed as exc:
            if self.on_denied == "retry":
                raise _model_retry(str(exc)) from exc
            raise

    async def _denied_as_retry(self, awaitable: Awaitable[Any]) -> Any:
        with self._denials():
            return await awaitable

    # -- toolsets ----------------------------------------------------------------
    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: Any,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one tool call through the capability (used by the wrapped toolset)."""
        call_id = getattr(ctx, "tool_call_id", None)
        self.counter.admit(name, arguments)
        self.bridge.sync(getattr(ctx, "usage", None))
        self.bridge.check(name)
        guarded = self._tools.get(name)
        if guarded is None:

            async def invoke(arguments: dict[str, Any]) -> Any:
                follow = _NEXT.get()
                assert follow is not None
                return await follow()

            guarded = self._tools.setdefault(name, self._python_wrap_tool(invoke, name))
        started = _core.monotonic()
        reset = _NEXT.set(call_next)
        try:
            with with_call_id(call_id or None):
                result = await self._denied_as_retry(guarded(arguments=arguments))
        except BaseException as exc:
            self._record(name, arguments, None, exc, started, call_id)
            raise
        finally:
            _NEXT.reset(reset)
        self._record(name, arguments, result, None, started, call_id)
        return result

    def wrap(self, toolset: Any) -> Any:
        """A pydantic-ai ``WrapperToolset`` around ``toolset`` whose calls go through this.

        Raises:
            ImportError: pydantic-ai is not installed.
        """
        return _wrapper_class()(toolset, capability=self)

    def toolset(self, *functions: Callable[..., Any]) -> Any:
        """A guarded ``FunctionToolset`` of plain tool functions (pydantic-ai required)."""
        toolsets = importlib.import_module("pydantic_ai.toolsets")
        return self.wrap(toolsets.FunctionToolset(list(functions)))

    # -- runs ---------------------------------------------------------------------
    def reset(self) -> None:
        """Start a new run: zero the call counters and the usage reading."""
        self.counter.reset()
        self.bridge.reset()

    def finish(self, result: Any) -> Any:
        """Feed a run result's final usage to the budget and the tape; returns the spend."""
        model = None
        response = getattr(result, "response", None)
        if response is not None:
            found = getattr(response, "model_name", None)
            model = found if isinstance(found, str) else None
        spend = self.bridge.sync(result, model)
        record_llm(self.tape, framework=FRAMEWORK, model=model, tokens=usage_totals(result))
        return spend

    @contextlib.contextmanager
    def _scope(self) -> Iterator[None]:
        self.reset()
        with contextlib.ExitStack() as stack:
            if self.guard is not None and current_run() is None:
                stack.enter_context(self.guard)
            if self.budget is not None and getattr(self.budget, "per", None) == "run":
                try:
                    self.budget.spent()
                except RuntimeError:
                    stack.enter_context(self.budget.run())
            self.bridge.check(FRAMEWORK)
            yield

    def run_sync(self, agent: Any, prompt: Any, /, **options: Any) -> Any:
        """``agent.run_sync(prompt, **options)`` inside this capability's run scope."""
        with self._scope():
            result = agent.run_sync(prompt, **options)
            self.finish(result)
            return result

    async def run(self, agent: Any, prompt: Any, /, **options: Any) -> Any:
        """``await agent.run(prompt, **options)`` inside this capability's run scope."""
        with self._scope():
            result = await agent.run(prompt, **options)
            self.finish(result)
            return result


def guard_tool(func: F | None = None, /, **options: Any) -> Any:
    """Shorthand for ``PythonWrapCapability(**options).tool(func)``."""
    name = options.pop("name", None)
    capability = PythonWrapCapability(**options)
    if func is None:
        return functools.partial(capability.tool, name=name)
    return capability.tool(func, name=name)


def _model_retry(message: str) -> Exception:
    model_retry = importlib.import_module("pydantic_ai").ModelRetry
    return cast(Exception, model_retry(message))


@functools.lru_cache(maxsize=1)
def _wrapper_class() -> Any:
    import dataclasses

    base: Any = importlib.import_module("pydantic_ai.toolsets").WrapperToolset

    @dataclasses.dataclass
    class PythonWrapToolset(base):  # type: ignore[misc]
        """A toolset whose every call goes through a `PythonWrapCapability`."""

        capability: Any = None

        async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
            parent = super().call_tool

            def follow() -> Awaitable[Any]:
                return cast(Awaitable[Any], parent(name, tool_args, ctx, tool))

            return await self.capability.call(name, tool_args, ctx, follow)

    return PythonWrapToolset
