"""python-wrap for the OpenAI Agents SDK: guarded function tools and run hooks.

The SDK (``agents``) is never imported at module level. `AgentsGuard` holds the settings
and gives you:

* `AgentsGuard.function_tool` - ``agents.function_tool`` with python-wrap `tool`
  semantics inside: per-attempt ``timeout``, ``retry``, `ToolPolicy` checks and
  approval, and replay safety through an `EffectLedger` keyed on the SDK's
  ``tool_call_id``;
* `AgentsGuard.wrap` - the same guards (limits, ledger, timeout, audit) around an
  existing ``FunctionTool``, such as the one ``Agent.as_tool()`` returns for a
  sub-agent;
* `AgentsGuard.hooks` / `AgentsGuard.agent_hooks` - ``RunHooks`` / ``AgentHooks`` that
  feed every model response's usage into a python-wrap `Budget` (refusing the next model
  call once it is spent), write tool and model calls to an `AuditTape`, and count tool
  calls against ``max_tool_calls`` and ``max_repeats``.

Sub-agents used as tools run their own ``Runner.run`` with their own ``max_turns``, so
nothing in the SDK bounds the total work of an agent tree. The guard's counters are
shared by every tool that passes through it, at any depth: wrap the sub-agent tools with
`AgentsGuard.wrap` (and give sub-agents ``hooks=guard.agent_hooks()``) and one
``max_tool_calls`` caps the whole tree.

Errors that should end the run (`LoopDetected`, `StepLimitExceeded`, a spent budget, an
`EffectUnknown`...) are re-raised even though the SDK turns tool exceptions into a
message for the model, also through sub-agent runs that would swallow them; a refused
tool (`ToolNotAllowed`) or a tool timeout is reported to the model as a tool error. The
SDK wraps a re-raised error in its ``UserError``; `AgentsGuard.run` / `run_sync` unwrap it
(`fatal_cause` does it for your own ``Runner.run`` calls).
"""

from __future__ import annotations

import contextvars
import copy
import functools
import importlib
import types
from collections.abc import Callable, Iterable
from typing import Any, TypeVar, cast

from python_wrap import _core
from python_wrap.adapters._frameworks import (
    _LAYER_ATTR,
    FATAL,
    CallCounter,
    UsageBridge,
    record_llm,
    record_tool,
    usage_totals,
)
from python_wrap.adapters.pydantic_ai import _matcher, compose
from python_wrap.agents import (
    EffectLedger,
    ToolPolicy,
    _effect_key,
    canonical,
    current_run,
    run_id_var,
    tool,
    with_call_id,
)

__all__ = ["AgentsGuard", "fatal_cause"]

F = TypeVar("F", bound=Callable[..., Any])
FRAMEWORK = "openai-agents"
_GUARDED_ATTR = "__python_wrap_guarded__"


class _Box:
    """Collects run-ending errors raised inside the SDK's own error handling."""

    __slots__ = ("error", "parent")

    def __init__(self, parent: _Box | None = None) -> None:
        self.error: BaseException | None = None
        self.parent = parent

    def mark(self, error: BaseException) -> None:
        """Record ``error`` here and in every enclosing guarded tool (sub-agent runs)."""
        box: _Box | None = self
        while box is not None:
            if box.error is None:
                box.error = error
            box = box.parent


def _mark_fatal(error: BaseException) -> None:
    box = _BOX.get()
    if box is not None:
        box.mark(error)


def fatal_cause(error: BaseException, fatal: tuple[type[BaseException], ...]) -> BaseException:
    """The run-ending python-wrap error inside an SDK wrapper exception, else ``error``."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, fatal):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return error


_BOX: contextvars.ContextVar[_Box | None] = contextvars.ContextVar(
    "python_wrap_openai_agents_box", default=None
)


class AgentsGuard:
    """python-wrap's tool safety, budget, audit and loop guards for the OpenAI Agents SDK.

    Args:
        timeout: Per-attempt time limit for every tool (seconds or ``"30s"``); None
            leaves them unbounded (`doctor` reports PW020 for such tools).
        retry: A retry decorator applied to each function tool, inside the ledger.
        ledger: An `EffectLedger`: a call replayed with the same ``tool_call_id``
            returns the stored result instead of running again.
        read_only: Glob patterns of tools without side effects.
        approval: Glob patterns of tools that need the approver's yes (``ToolPolicy`` of
            the current run, or ``policy``).
        policy: A `ToolPolicy` attached to every guarded run (`run` / `run_sync`).
        budget: A python-wrap `Budget` fed by the hooks with each response's usage.
        tape: An `AuditTape` receiving tool calls and model responses.
        max_tool_calls: Most tool calls through this guard, sub-agents included
            (`StepLimitExceeded`); `reset` (or `run`) starts a new count.
        max_repeats: Most identical tool calls (same name and arguments).
        fatal: Exception types that end the run instead of being shown to the model.
        model: Model name used to price usage that does not name one.

    Example:
        ```python
        from python_wrap import EffectLedger
        from python_wrap.adapters.openai_agents import AgentsGuard

        guard = AgentsGuard(timeout=10, ledger=EffectLedger(), max_tool_calls=20)
        hooks = guard.hooks()  # a plain object with the RunHooks methods without the SDK
        assert callable(hooks.on_tool_start) and callable(hooks.on_llm_end)
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
        budget: Any = None,
        tape: Any = None,
        max_tool_calls: int | None = None,
        max_repeats: int | None = None,
        fatal: tuple[type[BaseException], ...] = FATAL,
        model: str | None = None,
    ) -> None:
        if ledger is not None and not isinstance(ledger, EffectLedger):
            raise TypeError("ledger must be an EffectLedger")
        if policy is not None and not isinstance(policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        self.timeout = timeout
        self.retry = retry
        self.ledger = ledger
        self.policy = policy
        self.budget = budget
        self.tape = tape
        self.fatal = fatal
        self.model = model
        self.counter = CallCounter(max_tool_calls, max_repeats)
        self.bridge = UsageBridge(budget, model)
        self._read_only = _matcher(read_only)
        self._approval = _matcher(approval)

    # -- tools ----------------------------------------------------------------
    def _catch_fatal(self, func: Callable[..., Any], box_of: Callable[[], _Box | None]) -> Any:
        fatal = self.fatal
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_inner(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await func(*args, **kwargs)
                except fatal as exc:
                    box = box_of()
                    if box is not None:
                        box.mark(exc)
                    raise

            return async_inner

        @functools.wraps(func)
        def inner(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except fatal as exc:
                box = box_of()
                if box is not None:
                    box.mark(exc)
                raise

        return inner

    def guard_function(self, func: F, *, name: str | None = None) -> F:
        """``func`` with python-wrap `tool` semantics, ready for ``agents.function_tool``.

        Keeps the signature, annotations and docstring (the SDK builds the schema from
        them). A first ``RunContextWrapper`` / ``ToolContext`` parameter is left out of
        the argument digest.
        """
        label = name or func.__name__
        decorate = tool(
            name=label,
            read_only=self._read_only(label),
            requires_approval=self._approval(label),
            ledger=self.ledger,
            idempotent_key=None if self.ledger is not None else False,
        )
        guarded = decorate(compose(func, timeout=self.timeout, retry=self.retry))
        return cast(F, self._catch_fatal(guarded, _BOX.get))

    def function_tool(self, func: F | None = None, /, **sdk_options: Any) -> Any:
        """``agents.function_tool(func, **sdk_options)`` with the guards; needs the SDK."""
        if func is None:
            return functools.partial(self.function_tool, **sdk_options)
        sdk = importlib.import_module("agents")
        name = sdk_options.get("name_override") or func.__name__
        made = sdk.function_tool(self.guard_function(func, name=name), **sdk_options)
        return self._wrap(made, inner_guarded=True)

    def wrap(self, function_tool: Any) -> Any:
        """Guard an existing ``FunctionTool`` (``Agent.as_tool()`` included).

        Returns a copy whose ``on_invoke_tool`` counts the call, applies the ledger
        (keyed on the ``tool_call_id``), the timeout and the audit tape, then calls the
        original. Works on any object with ``name`` and ``on_invoke_tool``.
        """
        return self._wrap(function_tool, inner_guarded=False)

    def _wrap(self, function_tool: Any, *, inner_guarded: bool) -> Any:
        original = getattr(function_tool, "on_invoke_tool", None)
        name = getattr(function_tool, "name", None)
        if not callable(original) or not isinstance(name, str):
            raise TypeError("expected a FunctionTool (an object with name and on_invoke_tool)")
        guard = self

        async def call_original(ctx: Any, payload: Any) -> Any:
            result = original(ctx, payload)
            if hasattr(result, "__await__"):
                result = await result
            return result

        limited = call_original
        if not inner_guarded and self.timeout is not None:
            limited = compose(call_original, timeout=self.timeout, retry=None)

        async def on_invoke_tool(ctx: Any, payload: Any) -> Any:
            try:
                return await invoke(ctx, payload)
            except guard.fatal as exc:
                _mark_fatal(exc)  # a sub-agent run may swallow it: tell the enclosing tools
                raise

        async def invoke(ctx: Any, payload: Any) -> Any:
            call_id = getattr(ctx, "tool_call_id", None) or None
            guard.counter.admit(name, payload)
            guard.bridge.check(name)
            if not inner_guarded:
                run = current_run()
                if run is not None:
                    run.step("tool", name, {"json": payload})
            started = _core.monotonic()
            box = _Box(_BOX.get())
            reset = _BOX.set(box)
            try:
                with with_call_id(call_id):
                    if not inner_guarded and guard.ledger is not None:
                        digest = canonical({"json": payload})
                        result = await guard.ledger.aexecute(
                            name,
                            limited,
                            (ctx, payload),
                            key=_effect_key(name, call_id, digest, run_id_var.get(), None),
                            digest=digest,
                            call_id=call_id,
                        )
                    else:
                        result = await limited(ctx, payload)
            except BaseException as exc:
                guard._record(name, payload, None, exc, started, call_id)
                raise
            finally:
                _BOX.reset(reset)
            if box.error is not None:
                guard._record(name, payload, None, box.error, started, call_id)
                raise box.error
            guard._record(name, payload, result, None, started, call_id)
            return result

        guarded = copy.copy(function_tool)
        guarded.on_invoke_tool = on_invoke_tool
        setattr(guarded, _GUARDED_ATTR, True)
        setattr(
            guarded,
            _LAYER_ATTR,
            (
                "framework_tool",
                {
                    "framework": FRAMEWORK,
                    "name": name,
                    # The SDK's own FunctionTool(timeout_seconds=...) counts too.
                    "timeout": self.timeout
                    if self.timeout is not None
                    else getattr(function_tool, "timeout_seconds", None),
                },
            ),
        )
        return guarded

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

    # -- hooks ----------------------------------------------------------------
    async def _llm_start(self, agent: Any) -> None:
        self.bridge.check(getattr(agent, "name", None) or FRAMEWORK)

    async def _llm_end(self, agent: Any, response: Any) -> None:
        tokens = usage_totals(response)
        model = getattr(agent, "model", None)
        model = model if isinstance(model, str) else self.model
        if tokens is not None:
            self.bridge.add(tokens[0], tokens[1], model)
        record_llm(self.tape, framework=FRAMEWORK, model=model, tokens=tokens)

    async def _tool_start(self, context: Any, tool_obj: Any) -> None:
        if getattr(tool_obj, _GUARDED_ATTR, False):
            return  # counted (and recorded) by the wrapper itself
        name = getattr(tool_obj, "name", None) or type(tool_obj).__name__
        arguments = getattr(context, "tool_arguments", None)
        self.counter.admit(name, arguments)
        run = current_run()
        if run is not None:
            run.step("tool", name, None if arguments is None else {"json": arguments})

    async def _tool_end(self, context: Any, tool_obj: Any, result: Any) -> None:
        if getattr(tool_obj, _GUARDED_ATTR, False):
            return
        name = getattr(tool_obj, "name", None) or type(tool_obj).__name__
        self._record(
            name,
            getattr(context, "tool_arguments", None),
            result,
            None,
            _core.monotonic(),
            getattr(context, "tool_call_id", None),
        )

    def hooks(self) -> Any:
        """``RunHooks`` for ``Runner.run(..., hooks=...)`` (an ``agents.RunHooks`` subclass
        when the SDK is installed, a plain object with the same methods otherwise)."""
        return _hook_class("RunHooks")(self)

    def agent_hooks(self) -> Any:
        """``AgentHooks`` for ``Agent(hooks=...)``: use it on sub-agents run as tools."""
        return _hook_class("AgentHooks")(self)

    # -- runs ---------------------------------------------------------------------
    def reset(self) -> None:
        """Start a new run: zero the call counters."""
        self.counter.reset()
        self.bridge.reset()

    async def run(self, agent: Any, prompt: Any, /, **options: Any) -> Any:
        """``await Runner.run(agent, prompt, hooks=self.hooks(), **options)`` (SDK needed)."""
        self.reset()
        sdk = importlib.import_module("agents")
        options.setdefault("hooks", self.hooks())
        try:
            return await sdk.Runner.run(agent, prompt, **options)
        except Exception as exc:
            found = fatal_cause(exc, self.fatal)
            if found is exc:
                raise
            raise found from exc

    def run_sync(self, agent: Any, prompt: Any, /, **options: Any) -> Any:
        """``Runner.run_sync(agent, prompt, hooks=self.hooks(), **options)`` (SDK needed)."""
        self.reset()
        sdk = importlib.import_module("agents")
        options.setdefault("hooks", self.hooks())
        try:
            return sdk.Runner.run_sync(agent, prompt, **options)
        except Exception as exc:
            found = fatal_cause(exc, self.fatal)
            if found is exc:
                raise
            raise found from exc


class _RunHooksMixin:
    """The ``RunHooks`` methods, delegating to an `AgentsGuard`."""

    def __init__(self, guard: AgentsGuard) -> None:
        self._guard = guard

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        return None

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        return None

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        return None

    async def on_llm_start(self, context: Any, agent: Any, *args: Any, **kwargs: Any) -> None:
        await self._guard._llm_start(agent)

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        await self._guard._llm_end(agent, response)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        await self._guard._tool_start(context, tool)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        await self._guard._tool_end(context, tool, result)


class _AgentHooksMixin(_RunHooksMixin):
    """The ``AgentHooks`` methods (``on_start`` / ``on_end`` instead of the agent ones)."""

    async def on_start(self, context: Any, agent: Any) -> None:
        return None

    async def on_end(self, context: Any, agent: Any, output: Any) -> None:
        return None

    async def on_handoff(self, context: Any, agent: Any, source: Any) -> None:
        return None


@functools.lru_cache(maxsize=4)
def _hook_class(kind: str) -> Any:
    mixin: type[_RunHooksMixin] = _AgentHooksMixin if kind == "AgentHooks" else _RunHooksMixin
    try:
        base = getattr(importlib.import_module("agents"), kind)
    except (ImportError, AttributeError):
        return mixin
    # ``agents.RunHooks`` is a parametrised generic alias: new_class resolves it.
    return types.new_class(f"PythonWrap{kind}", (mixin, base))
