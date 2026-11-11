"""validated(): structured-output validation with capped, error-only repairs."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from python_wrap.llm import Budget
from python_wrap.llm_testing import fake_llm
from python_wrap.llm_validate import ValidationRepairExhausted, validated


def parse_city(text: str) -> dict[str, str]:
    data = json.loads(text)
    if not isinstance(data, dict) or "city" not in data:
        raise ValueError("missing required field 'city'")
    return {"city": str(data["city"])}


class Scripted:
    """A model that replays a fixed list of replies and records every call."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


def user(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


class TestRepairs:
    def test_valid_first_time_makes_one_call(self) -> None:
        model = Scripted(['{"city": "Oslo"}'])
        ask = validated(parse_city)(model)
        assert ask(messages=user("where")) == {"city": "Oslo"}
        assert len(model.calls) == 1

    def test_error_only_repair_appends_reply_and_error(self) -> None:
        model = Scripted(['{"town": "x"}', '{"city": "Oslo"}'])
        ask = validated(parse_city)(model)
        assert ask(messages=user("where")) == {"city": "Oslo"}
        first, second = (c[1]["messages"] for c in model.calls)
        assert first == user("where")
        assert second[:1] == user("where")
        assert second[1] == {"role": "assistant", "content": '{"town": "x"}'}
        assert second[2]["role"] == "user"
        assert "missing required field 'city'" in second[2]["content"]
        assert len(second[2]["content"]) < 200  # only the error, not the prompt again

    def test_full_repair_restates_previous_reply_in_one_turn(self) -> None:
        model = Scripted(["nope", '{"city": "Oslo"}'])
        ask = validated(parse_city, repair="full")(model)
        ask(messages=user("where"))
        extra = model.calls[1][1]["messages"][1:]
        assert [m["role"] for m in extra] == ["user"]
        assert "nope" in extra[0]["content"] and "Expecting value" in extra[0]["content"]

    def test_prompt_style_calls(self) -> None:
        model = Scripted(["x", '{"city": "Oslo"}'])
        ask = validated(parse_city)(model)
        ask(prompt="where?")
        assert model.calls[1][1]["prompt"].startswith("where?")
        positional = Scripted(["x", '{"city": "Oslo"}'])
        validated(parse_city)(positional)("where?")
        assert (
            positional.calls[1][0][0].startswith("where?") and "failed" in positional.calls[1][0][0]
        )

    def test_exhaustion_reports_attempts_and_cost(self) -> None:
        llm = fake_llm(["bad", "worse", "still bad", "never reached"], usage=(10, 5))
        ask = validated(parse_city, max_repairs=2)(lambda messages: llm.complete(prompt="p"))
        with pytest.raises(ValidationRepairExhausted) as info:
            ask(messages=user("q"))
        exc = info.value
        assert llm.call_count == 3
        assert [a.number for a in exc.attempts] == [1, 2, 3]
        assert [a.output for a in exc.attempts] == ["bad", "worse", "still bad"]
        assert all(a.error for a in exc.attempts)
        assert exc.tokens == 45 and exc.reason == "max_repairs"
        assert exc.last_error and "attempt(s)" in str(exc)

    def test_zero_repairs_validates_once(self) -> None:
        model = Scripted(["bad"])
        with pytest.raises(ValidationRepairExhausted) as info:
            validated(parse_city, max_repairs=0)(model)(messages=user("q"))
        assert len(model.calls) == 1 and len(info.value.attempts) == 1

    def test_call_errors_propagate_without_repair(self) -> None:
        def boom(messages: Any) -> Any:
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            validated(parse_city)(boom)(messages=user("q"))

    def test_events_emitted(self) -> None:
        from python_wrap import _core

        names: list[str] = []
        _core.listeners.append(lambda e: names.append(e.name))
        try:
            with pytest.raises(ValidationRepairExhausted):
                validated(parse_city, max_repairs=1)(Scripted(["x"]))(messages=user("q"))
        finally:
            _core.listeners.pop()
        assert names.count("llm.validation_failed") == 2
        assert "llm.repair_exhausted" in names

    def test_error_text_is_capped_and_single_line(self) -> None:
        def parse(text: str) -> None:
            raise ValueError("line one\n" + "x" * 5000)

        model = Scripted(["a"])
        with pytest.raises(ValidationRepairExhausted):
            validated(parse, max_repairs=1, max_error_chars=100)(model)(messages=user("q"))
        sent = model.calls[1][1]["messages"][-1]["content"]
        assert "\n" not in sent.split("failed validation: ", 1)[1].split("\nReply")[0]
        assert len(sent) < 250

    def test_markdown_fences_are_stripped(self) -> None:
        model = Scripted(['```json\n{"city": "Oslo"}\n```'])
        assert validated(parse_city)(model)(messages=user("q")) == {"city": "Oslo"}
        raw = Scripted(['```json\n{"city": "Oslo"}\n```'])
        with pytest.raises(ValidationRepairExhausted):
            validated(parse_city, unfence=False, max_repairs=0)(raw)(messages=user("q"))

    def test_custom_reask(self) -> None:
        seen: list[tuple[str, str]] = []

        def reask(args: tuple[Any, ...], kwargs: dict[str, Any], previous: str, error: str) -> Any:
            seen.append((previous, error))
            return args, {**kwargs, "hint": error}

        model = Scripted(["x", '{"city": "Oslo"}'])
        validated(parse_city, reask=reask)(model)(topic="t")
        assert seen and model.calls[1][1]["hint"] == seen[0][1]

    def test_unsupported_call_shape_is_explained(self) -> None:
        with pytest.raises(TypeError, match="reask="):
            validated(parse_city)(Scripted(["x"]))(topic=1)


class TestParsers:
    def test_pydantic_like_class_is_used_through_model_validate_json(self) -> None:
        class Model:
            @classmethod
            def model_validate_json(cls, text: str) -> str:
                if "ok" not in text:
                    raise ValueError("not ok")
                return "parsed"

        assert validated(Model)(Scripted(["ok"]))(messages=user("q")) == "parsed"

    def test_non_callable_parse_rejected(self) -> None:
        with pytest.raises(TypeError):
            validated(5)

    def test_any_exception_from_parse_counts(self) -> None:
        def parse(text: str) -> str:
            raise KeyError("k")

        with pytest.raises(ValidationRepairExhausted):
            validated(parse, max_repairs=0)(Scripted(["x"]))(messages=user("q"))

    @pytest.mark.parametrize(
        "response",
        [
            "plain",
            {"text": "plain"},
            {"content": "plain"},
            {"content": [{"type": "text", "text": "pla"}, {"type": "text", "text": "in"}]},
            {"choices": [{"message": {"content": "plain"}}]},
            SimpleNamespace(text="plain"),
            SimpleNamespace(content=[SimpleNamespace(text="plain")]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="plain"))]),
            SimpleNamespace(output_text="plain"),
            b"plain",
        ],
    )
    def test_reply_text_shapes(self, response: Any) -> None:
        assert validated(lambda t: t.upper(), max_repairs=0)(lambda **kw: response)() == "PLAIN"

    def test_custom_text_reader_and_unreadable_reply(self) -> None:
        assert validated(str.upper, text=lambda r: r["x"])(lambda **kw: {"x": "ok"})() == "OK"
        with pytest.raises(TypeError, match="text="):
            validated(str.upper)(lambda **kw: 42)()


class TestCaps:
    def test_max_tokens_stops_repairs_early(self) -> None:
        llm = fake_llm(["a", "b", "c", "d"], usage=(50, 50))
        ask = validated(parse_city, max_repairs=5, max_tokens=150)(
            lambda messages: llm.complete(prompt="p")
        )
        with pytest.raises(ValidationRepairExhausted) as info:
            ask(messages=user("q"))
        assert info.value.reason == "max_tokens"
        assert llm.call_count == 2  # 100 tokens, then 200 >= 150

    def test_budget_exhaustion_stops_repairs(self) -> None:
        budget = Budget(tokens=150)
        llm = fake_llm(["a", "b", "c", "d"], usage=(50, 50))

        @validated(parse_city, max_repairs=5, budget=budget)
        @budget
        def ask(messages: Any) -> Any:
            return llm.complete(prompt="p")

        with pytest.raises(ValidationRepairExhausted) as info:
            ask(messages=user("q"))
        assert info.value.reason == "budget"
        assert llm.call_count == 2

    def test_budget_key(self) -> None:
        budget = Budget(tokens=50, per="key", key=lambda messages, who: who)
        llm = fake_llm(["a", "b", "c"], usage=(40, 40))

        @validated(parse_city, max_repairs=3, budget=budget, budget_key=lambda messages, who: who)
        @budget
        def ask(messages: Any, who: str) -> Any:
            return llm.complete(prompt="p")

        with pytest.raises(ValidationRepairExhausted) as info:
            ask(user("q"), "alice")
        assert info.value.reason == "budget"

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_repairs": -1}, {"max_repairs": True}, {"repair": "x"}, {"max_tokens": 0}],
    )
    def test_bad_options(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            validated(parse_city, **kwargs)

    @given(
        max_repairs=st.integers(0, 6),
        good_at=st.one_of(st.none(), st.integers(1, 10)),
    )
    def test_property_calls_are_bounded(self, max_repairs: int, good_at: int | None) -> None:
        calls = 0

        def model(messages: Any) -> str:
            nonlocal calls
            calls += 1
            return '{"city": "x"}' if good_at is not None and calls >= good_at else "bad"

        ask = validated(parse_city, max_repairs=max_repairs)(model)
        try:
            ask(messages=user("q"))
            assert good_at is not None and good_at <= max_repairs + 1
        except ValidationRepairExhausted as exc:
            assert len(exc.attempts) == calls == max_repairs + 1
            assert good_at is None or good_at > max_repairs + 1
        assert calls <= max_repairs + 1


class TestAsync:
    def test_async_repair(self) -> None:
        replies = iter(["bad", '{"city": "Oslo"}'])
        seen: list[Any] = []

        @validated(parse_city)
        async def ask(messages: Any) -> str:
            seen.append(messages)
            await asyncio.sleep(0)
            return next(replies)

        assert asyncio.run(ask(messages=user("q"))) == {"city": "Oslo"}
        assert len(seen) == 2 and seen[1][-2]["role"] == "assistant"

    def test_async_exhaustion(self) -> None:
        @validated(parse_city, max_repairs=1)
        async def ask(messages: Any) -> str:
            return "bad"

        with pytest.raises(ValidationRepairExhausted) as info:
            asyncio.run(ask(messages=user("q")))
        assert len(info.value.attempts) == 2

    def test_wrapped_function_keeps_its_name(self) -> None:
        @validated(parse_city)
        async def ask_it(messages: Any) -> str:
            return ""

        assert ask_it.__name__ == "ask_it"
        assert inspect.iscoroutinefunction(ask_it)
