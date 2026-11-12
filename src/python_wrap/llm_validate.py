"""Validate structured LLM output and repair it with a cheap, capped re-ask.

`validated` wraps a function that calls a model. The reply text is handed to your
``parse`` callable (``Model.model_validate_json``, a JSON-schema checker, ``json.loads``
plus your own checks: anything that raises on bad output). When it raises, the call is
repeated with only the error text added to the conversation (``repair="error_only"``), up
to ``max_repairs`` times and never past an optional token or `Budget` cap. When repairs
run out, `ValidationRepairExhausted` carries every attempt and what they cost.

Nothing here imports pydantic or a provider SDK: ``parse`` is any callable, and a class
with ``model_validate_json`` (pydantic v2) is accepted directly.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, ParamSpec, TypeVar, cast

from python_wrap import _core
from python_wrap.errors import PythonWrapError
from python_wrap.llm import Usage, _usage_of

__all__ = ["Attempt", "ValidationRepairExhausted", "validated"]

P = ParamSpec("P")
T = TypeVar("T")

RepairMode = Literal["error_only", "full"]
_REPAIRS = ("error_only", "full")
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class Attempt:
    """One model call and how its output validated.

    Attributes:
        number: 1 for the first call, 2 for the first repair, and so on.
        output: The text that was validated (empty when none could be read).
        error: The validation error text, or None for the accepted attempt.
        usage: Tokens the call used, or None when the reply carried no usage.
    """

    number: int
    output: str
    error: str | None
    usage: Usage | None


class ValidationRepairExhausted(PythonWrapError):
    """The model kept returning output that fails validation.

    Attributes:
        attempts: Every `Attempt`, in order.
        tokens: Total tokens used by the attempts that reported usage.
        reason: ``"max_repairs"``, ``"max_tokens"`` or ``"budget"``: what stopped the loop.
        last_error: The final validation error text.
    """

    def __init__(self, function: str, attempts: list[Attempt], reason: str) -> None:
        self.attempts = list(attempts)
        self.reason = reason
        self.tokens = sum(a.usage.total_tokens for a in attempts if a.usage is not None)
        self.last_error = attempts[-1].error or "" if attempts else ""
        super().__init__(
            f"{function}: output still invalid after {len(attempts)} attempt(s) "
            f"({reason}, {self.tokens} tokens): {self.last_error[:200]}"
        )


def _text_of(response: Any) -> str:
    """Pull the reply text out of a string, dict or SDK-style response."""
    if isinstance(response, str):
        return response
    if isinstance(response, (bytes, bytearray)):
        return bytes(response).decode("utf-8", "replace")
    pieces: list[str] = []

    def field(obj: Any, name: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(name)
        return getattr(obj, name, None)

    direct = field(response, "text")
    if isinstance(direct, str):
        return direct
    direct = field(response, "output_text")
    if isinstance(direct, str):
        return direct
    content = field(response, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        for block in content:
            text = block if isinstance(block, str) else field(block, "text")
            if isinstance(text, str):
                pieces.append(text)
        if pieces:
            return "".join(pieces)
    choices = field(response, "choices")
    if isinstance(choices, (list, tuple)) and choices:
        message = field(choices[0], "message")
        text = field(message, "content") if message is not None else field(choices[0], "text")
        if isinstance(text, str):
            return text
    raise TypeError(
        f"cannot find text in a {type(response).__name__}; pass text= to validated() to read it"
    )


def _unfence(text: str) -> str:
    found = _FENCE.match(text)
    return found.group(1) if found else text


def _resolve_parse(parse: Any) -> Callable[[str], Any]:
    validate = getattr(parse, "model_validate_json", None)
    if callable(validate):
        return cast("Callable[[str], Any]", validate)
    if callable(parse):
        return cast("Callable[[str], Any]", parse)
    raise TypeError(
        f"parse must be a callable or a class with model_validate_json, got {type(parse)!r}"
    )


def _error_text(exc: BaseException, limit: int) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _repair_message(mode: str, error: str, previous: str) -> str:
    if mode == "full":
        return (
            "Your previous reply failed validation.\n"
            f"Previous reply:\n{previous}\n"
            f"Validation error:\n{error}\n"
            "Reply again with the complete corrected output only."
        )
    return f"That reply failed validation: {error}\nReply with the corrected output only."


def _default_reask(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    previous: str,
    error: str,
    mode: str,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Extend a chat ``messages=`` list (or a ``prompt=`` string) with the repair turn."""
    message = _repair_message(mode, error, previous)
    messages = kwargs.get("messages")
    if isinstance(messages, (list, tuple)):
        extra: list[Any] = []
        if mode == "error_only":
            extra.append({"role": "assistant", "content": previous})
        extra.append({"role": "user", "content": message})
        return args, {**kwargs, "messages": [*messages, *extra]}
    prompt = kwargs.get("prompt")
    if isinstance(prompt, str):
        return args, {**kwargs, "prompt": f"{prompt}\n\n{previous}\n\n{message}"}
    if args and isinstance(args[0], str) and "prompt" not in kwargs:
        return (f"{args[0]}\n\n{previous}\n\n{message}", *args[1:]), kwargs
    raise TypeError(
        "validated() can only re-ask calls that take messages=[...] or a prompt string; "
        "pass reask=(args, kwargs, previous, error) -> (args, kwargs) for other shapes"
    )


class _Plan:
    def __init__(
        self,
        parse: Any,
        max_repairs: int,
        repair: str,
        text: Callable[[Any], str] | None,
        usage: Callable[[Any], Any] | None,
        reask: Callable[..., tuple[tuple[Any, ...], dict[str, Any]]] | None,
        max_tokens: int | None,
        budget: Any,
        budget_key: Callable[..., Any] | None,
        max_error_chars: int,
        unfence: bool,
    ) -> None:
        if isinstance(max_repairs, bool) or not isinstance(max_repairs, int) or max_repairs < 0:
            raise ValueError(f"max_repairs must be an integer >= 0, got {max_repairs!r}")
        if repair not in _REPAIRS:
            raise ValueError(f"repair must be one of {', '.join(_REPAIRS)}; got {repair!r}")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            raise ValueError(f"max_tokens must be an integer >= 1, got {max_tokens!r}")
        if max_error_chars < 20:
            raise ValueError("max_error_chars must be >= 20")
        self.parse = _resolve_parse(parse)
        self.max_repairs, self.repair = max_repairs, repair
        self.text = text or _text_of
        self.usage, self.reask = usage, reask
        self.max_tokens, self.budget, self.budget_key = max_tokens, budget, budget_key
        self.max_error_chars, self.unfence = max_error_chars, unfence

    def read(self, response: Any) -> tuple[str, Usage | None]:
        try:
            text = self.text(response)
        except Exception as exc:
            if isinstance(exc, TypeError):
                raise
            text = ""
        return text, _usage_of(response, self.usage)

    def stop_reason(
        self, attempts: list[Attempt], args: tuple[Any, ...], kw: dict[str, Any]
    ) -> str:
        """Why no further repair may run ('' when one may)."""
        if len(attempts) > self.max_repairs:
            return "max_repairs"
        spent = sum(a.usage.total_tokens for a in attempts if a.usage is not None)
        if self.max_tokens is not None and spent >= self.max_tokens:
            return "max_tokens"
        if self.budget is not None:
            key = self.budget_key(*args, **kw) if self.budget_key is not None else None
            if self.budget.exhausted(key=key) if key is not None else self.budget.exhausted():
                return "budget"
        return ""

    def check(self, text: str) -> tuple[Any, str | None]:
        try:
            return self.parse(_unfence(text) if self.unfence else text), None
        except Exception as exc:
            return None, _error_text(exc, self.max_error_chars)

    def next_call(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], text: str, error: str
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if self.reask is not None:
            return self.reask(args, kwargs, text, error)
        return _default_reask(args, kwargs, text, error, self.repair)


def validated(
    parse: Any,
    *,
    max_repairs: int = 2,
    repair: RepairMode = "error_only",
    text: Callable[[Any], str] | None = None,
    usage: Callable[[Any], Any] | None = None,
    reask: Callable[..., tuple[tuple[Any, ...], dict[str, Any]]] | None = None,
    max_tokens: int | None = None,
    budget: Any = None,
    budget_key: Callable[..., Any] | None = None,
    max_error_chars: int = 600,
    unfence: bool = True,
    name: str | None = None,
) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """Validate a model call's output and repair it with at most ``max_repairs`` re-asks.

    The wrapped function makes one model call and returns its response (a string, a dict
    or an SDK object). The decorator reads the reply text, runs ``parse`` on it and:

    - returns the parsed value when ``parse`` succeeds;
    - otherwise calls the function again with a repair turn added, up to
      ``max_repairs`` times, and raises `ValidationRepairExhausted` after that.

    There are never more than ``1 + max_repairs`` calls, and a repair is skipped (the
    error raised with ``reason="max_tokens"`` / ``"budget"``) once the attempts' tokens
    reach ``max_tokens`` or the ``budget`` is exhausted. Exceptions from the call itself
    (including a `BudgetExceeded` from a decorated function) propagate untouched.

    Args:
        parse: ``parse(text) -> value``, raising on invalid output (any exception counts).
            A class with ``model_validate_json`` is used through that method.
        max_repairs: Re-asks allowed after the first call; 0 validates without repairing.
        repair: ``"error_only"`` continues the chat with the model's reply and just the
            error text (cheapest); ``"full"`` restates the previous reply and the error in
            one user turn (for prompt-style APIs). Both add the turn to ``messages=``
            (a list) or to ``prompt=`` / the first string argument.
        text: ``text(response) -> str`` when the reply is not a string, ``.text``,
            ``.content`` blocks, ``.choices[0].message.content`` or a dict of those.
        usage: ``usage(response)`` for token counts `read_usage` cannot find.
        reask: ``reask(args, kwargs, previous_text, error) -> (args, kwargs)`` to build the
            repair call yourself (needed for call shapes other than the above).
        max_tokens: Stop repairing once the attempts used this many tokens in total.
        budget: A `python_wrap.llm.Budget` (anything with ``exhausted()``) that must not be
            exhausted for a repair to run. Pair it with the same budget decorating the call
            so each attempt is charged.
        budget_key: ``budget_key(*args, **kwargs)`` for ``per="key"`` budgets.
        max_error_chars: Longest error text sent back to the model.
        unfence: Strip a surrounding markdown code fence before parsing.
        name: Label for events and errors; defaults to the function's name.

    Returns:
        A decorator for sync or async functions. The wrapped function returns the parsed
        value; it emits ``llm.validation_failed`` per failed attempt.

    Raises:
        ValidationRepairExhausted: When repairs ran out.

    Example:
        ```python
        import json

        from python_wrap.llm_validate import ValidationRepairExhausted, validated

        replies = iter(['{"city": ', '{"city": "Oslo"}'])  # truncated, then fixed


        def parse(text: str) -> dict[str, str]:
            data = json.loads(text)
            if "city" not in data:
                raise ValueError("missing 'city'")
            return {"city": data["city"]}


        @validated(parse, max_repairs=2)
        def ask(messages: list[dict[str, str]]) -> str:
            return next(replies)


        assert ask(messages=[{"role": "user", "content": "Where?"}]) == {"city": "Oslo"}
        ```
    """
    plan = _Plan(
        parse, max_repairs, repair, text, usage, reask, max_tokens, budget, budget_key,
        max_error_chars, unfence,
    )  # fmt: skip

    def decorate(func: Callable[P, Any]) -> Callable[P, Any]:
        label = name or _core.qualname(func)

        def failure(attempts: list[Attempt], reason: str) -> ValidationRepairExhausted:
            _core.emit("llm.repair_exhausted", label, attempts=len(attempts), reason=reason)
            return ValidationRepairExhausted(label, attempts, reason)

        def judged(
            response: Any, attempts: list[Attempt]
        ) -> tuple[Any, str | None, str, Usage | None]:
            output, used = plan.read(response)
            value, error = plan.check(output)
            attempts.append(Attempt(len(attempts) + 1, output, error, used))
            if error is not None:
                _core.emit("llm.validation_failed", label, attempt=len(attempts), error=error[:200])
            return value, error, output, used

        if _core.is_async(func):
            afunc = cast("Callable[..., Awaitable[Any]]", func)

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempts: list[Attempt] = []
                call_args, call_kwargs = args, kwargs
                while True:
                    response = await afunc(*call_args, **call_kwargs)
                    value, error, output, _ = judged(response, attempts)
                    if error is None:
                        return value
                    reason = plan.stop_reason(attempts, args, kwargs)
                    if reason:
                        raise failure(attempts, reason)
                    call_args, call_kwargs = plan.next_call(call_args, call_kwargs, output, error)

            return cast("Callable[P, Any]", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            attempts: list[Attempt] = []
            call_args: tuple[Any, ...] = args
            call_kwargs: dict[str, Any] = kwargs
            while True:
                response = func(*call_args, **call_kwargs)
                value, error, output, _ = judged(response, attempts)
                if error is None:
                    return value
                reason = plan.stop_reason(attempts, args, kwargs)
                if reason:
                    raise failure(attempts, reason)
                call_args, call_kwargs = plan.next_call(call_args, call_kwargs, output, error)

        return wrapper

    return decorate
