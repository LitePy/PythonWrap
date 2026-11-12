"""The SDK adapter scripts: offline paths run for real, the live paths skip cleanly."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from python_wrap import StepLimitExceeded
from python_wrap.testing import FakeClock

DIR = Path(__file__).resolve().parent.parent / "examples" / "sdk_adapters"
NAMES = ["openai_chat", "anthropic_messages", "pydantic_ai_agent"]
ENV = [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "PYDANTIC_AI_MODEL",
]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"sdk_adapters_{name}", DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", NAMES)
def test_without_key_the_script_skips_cleanly(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for variable in ENV:
        monkeypatch.delenv(variable, raising=False)
    assert load(name).main([]) == 0
    assert capsys.readouterr().out.startswith("SKIP")


@pytest.mark.parametrize("name", NAMES)
def test_sdk_is_not_imported_at_module_level(name: str) -> None:
    source = (DIR / f"{name}.py").read_text()
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any(
        ("openai" in line or "anthropic" in line or "pydantic_ai" in line) for line in top_level
    )


def test_openai_tool_loop_offline() -> None:
    result = load("openai_chat").run_offline()
    assert result["answer"] == "Order A-1 has shipped."
    assert result["requests"] == 2  # one tool round trip
    assert (
        result["tokens"] == 140
    )  # two calls, 70 tokens each, read from prompt_tokens/completion_tokens
    assert result["pong"] == "pong"


def test_openai_rate_limit_is_retried_after_the_provider_delay(fake_clock: FakeClock) -> None:
    result = load("openai_chat").run_offline(fail_first=True)
    assert result["answer"] == "Order A-1 has shipped."
    assert fake_clock.sleeps == [1.0]  # honoured retry-after


def test_anthropic_tool_loop_and_stream_offline() -> None:
    result = load("anthropic_messages").run_offline()
    assert result["answer"] == "We have 12 widgets."
    assert result["streamed"] == "Stock is healthy."
    assert result["tokens"] == 85 * 3  # two create calls plus the recorded stream usage


def test_anthropic_overloaded_is_retried(fake_clock: FakeClock) -> None:
    result = load("anthropic_messages").run_offline(fail_first=True)
    assert result["answer"] == "We have 12 widgets." and fake_clock.sleeps == [1.0]


def test_pydantic_ai_tools_are_replay_safe_and_the_run_is_guarded() -> None:
    module = load("pydantic_ai_agent")
    assert module.run_offline().startswith("done after 3 tool calls")
    assert module.NOTES == {"T-1": "sent the reset guide"}  # the repeated note ran once
    assert module.SEARCHES == ["reset password"]
    runaway = [("search_docs", {"query": f"q{n}"}) for n in range(20)]
    with pytest.raises(StepLimitExceeded):
        module.run_offline(runaway)
    assert len(module.SEARCHES) <= 3  # the paid-search quota held as well
