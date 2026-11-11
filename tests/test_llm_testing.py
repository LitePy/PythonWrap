from __future__ import annotations

import asyncio
import json

import pytest

from python_wrap import FakeProvider, FakeStream, fake_llm, retry
from python_wrap.chaos import FaultInjector, chaos
from python_wrap.llm_testing import (
    APITimeoutError,
    FakeProviderExhausted,
    OverloadedError,
    RateLimitError,
)
from python_wrap.testing import fake_clock

MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _clean() -> None:
    chaos.clear()
    yield  # type: ignore[misc]
    chaos.clear()


def test_scripted_replies_in_order_and_recorded() -> None:
    llm = fake_llm(["one", "two"], usage=(10, 3))
    assert llm.complete(messages=MSG, model="m").text == "one"
    assert str(llm.complete(messages=MSG)) == "two"
    assert llm.call_count == 2
    llm.assert_called_with(messages=MSG)
    llm.assert_tokens(input_tokens=20, output_tokens=6)
    assert llm.total_tokens == 26
    with pytest.raises(FakeProviderExhausted):
        llm.complete()
    with pytest.raises(AssertionError):
        llm.assert_called_with(messages=[])


def test_usage_styles_and_client_surfaces() -> None:
    a = fake_llm(["x"], usage=(4, 2))
    r = a.messages.create(model="m", messages=MSG)
    assert (r.usage.input_tokens, r.usage.output_tokens) == (4, 2)
    assert r.content[0].text == "x"
    o = fake_llm(["x"], usage=(4, 2), style="openai")
    r = o.chat.completions.create(model="m", messages=MSG)
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (4, 2)
    assert r.choices[0].message.content == "x"
    assert r.choices[0].finish_reason == "stop"
    with pytest.raises(ValueError):
        FakeProvider(style="bogus")


def test_scripted_errors_callables_and_dict_replies() -> None:
    llm = fake_llm([RateLimitError("slow", retry_after=2), ValueError, lambda c: c.prompt.upper()])
    with pytest.raises(RateLimitError) as info:
        llm.complete()
    assert info.value.status_code == 429
    assert info.value.response.status_code == 429
    assert info.value.response.headers["retry-after"] == "2"
    with pytest.raises(ValueError):
        llm.complete()
    assert llm.complete(messages=MSG).text == "HI"
    d = {"usage": {"prompt_tokens": 7, "completion_tokens": 1}}
    both = fake_llm([d])
    assert both.complete() is d
    both.assert_tokens(input_tokens=7, output_tokens=1)


def test_cycle_and_default_and_reset() -> None:
    assert [fake_llm(["a", "b"], cycle=True).complete().text for _ in range(1)] == ["a"]
    llm = fake_llm(["a", "b"], cycle=True)
    assert [llm.complete().text for _ in range(3)] == ["a", "b", "a"]
    llm = fake_llm(["a"], default="z")
    assert [llm.complete().text for _ in range(2)] == ["a", "z"]
    llm.reset()
    assert llm.call_count == 0 and llm.complete().text == "a"
    fake_llm().assert_not_called()


def test_streaming_delays_use_the_fake_clock() -> None:
    llm = fake_llm(["alpha beta gamma"], first_token_delay=0.5, chunk_delay=0.25)
    with fake_clock() as clock:
        chunks = [c.text for c in llm.stream(messages=MSG)]
    assert "".join(chunks) == "alpha beta gamma"
    assert len(chunks) == 3
    assert clock.total_slept == pytest.approx(0.5 + 0.75)


def test_async_complete_and_stream() -> None:
    llm = fake_llm(["one", "two three"], chunk_delay=1.0, delay=2.0)

    async def run() -> tuple[str, str]:
        first = (await llm.acomplete(messages=MSG)).text
        stream = await llm.astream(messages=MSG)
        parts = [c.choices[0].delta.content async for c in stream]
        return first, "".join(parts)

    with fake_clock() as clock:
        assert asyncio.run(run()) == ("one", "two three")
    assert clock.total_slept == pytest.approx(2.0 + 2.0)  # complete delay + 2 chunks


def test_async_surface_and_stream_flag() -> None:
    llm = fake_llm(["a b"])

    async def run() -> str:
        stream = await llm.aio.messages.create(messages=MSG, stream=True)
        return "".join([c.text async for c in stream])

    assert asyncio.run(run()) == "a b"
    assert llm.calls[0].stream is True
    assert "stream" not in llm.calls[0].kwargs


def test_fake_stream_error_after_chunks_and_validation() -> None:
    s = FakeStream(["a", "b", "c"], error=APITimeoutError, error_after=2)
    got: list[str] = []
    with pytest.raises(APITimeoutError):
        for c in s:
            got.append(c.text)
    assert got == ["a", "b"]
    assert [c.text for c in FakeStream(["a"])] == ["a"]  # re-iterable
    with pytest.raises(ValueError):
        FakeStream(["a"], chunk_delay=[1, 2])
    with pytest.raises(ValueError):
        FakeStream(["a"], error_after=5)


def test_rate_limit_fault_with_retry_and_times() -> None:
    llm = fake_llm(["done"])

    @retry(max_attempts=4, delay=1.0, on=RateLimitError)
    def ask() -> str:
        return llm.complete(messages=MSG).text

    with fake_clock() as clock, chaos.llm.rate_limit("llm.*", times=2, retry_after=1) as rule:
        assert ask() == "done"
        assert rule.injected == 2
    assert llm.call_count == 3 and llm.served_count == 1
    assert clock.sleeps == [1.0, 1.0]
    llm.assert_tokens(output_tokens=1)


def test_status_faults_are_duck_typed() -> None:
    inj = FaultInjector()
    llm = fake_llm(["x"], injector=inj)
    inj.llm.overloaded("llm.fake", times=1)
    with pytest.raises(OverloadedError) as e1:
        llm.complete()
    assert e1.value.status_code == 529 and type(e1.value).__name__ == "OverloadedError"
    inj.llm.server_error("llm.fake", status=503, times=1)
    with pytest.raises(Exception) as e2:
        llm.complete()
    assert e2.value.status_code == 503  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        inj.llm.server_error("llm.fake", status=404)
    inj.llm.timeout("llm.fake", after=3, times=1)
    with fake_clock() as clock, pytest.raises(TimeoutError):
        llm.complete()
    assert clock.sleeps == [3.0]
    # each injection raises a fresh instance
    inj.llm.overloaded("llm.fake")
    seen = []
    for _ in range(2):
        with pytest.raises(OverloadedError) as e:
            llm.complete()
        seen.append(e.value)
    assert seen[0] is not seen[1]


def test_malformed_json_refusal_and_invalid_tool_call() -> None:
    inj = FaultInjector()
    llm = fake_llm(["never"], injector=inj)
    inj.llm.malformed_json("llm.*", text='{"a": [1, 2, 3, 4]}', keep=0.5, times=1)
    r = llm.complete()
    assert r.stop_reason == "max_tokens" and r.text == '{"a": [1,'
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.text)
    inj.llm.refusal("llm.*", message="no", times=1)
    r = llm.complete()
    assert r.stop_reason == "refusal" and r.choices[0].finish_reason == "content_filter"
    assert r.choices[0].message.refusal == "no"
    inj.llm.invalid_tool_call("llm.*", tool="lookup", style="openai", times=1)
    r = llm.complete()
    fn = r.choices[0].message.tool_calls[0].function
    assert fn.name == "lookup"
    with pytest.raises(json.JSONDecodeError):
        json.loads(fn.arguments)
    assert r.content[0].input == {}
    inj.llm.invalid_tool_call("llm.*", arguments={"n": "seven"}, times=1)
    assert llm.complete().content[0].input == {"n": "seven"}
    assert llm.complete().text == "never"
    with pytest.raises(ValueError):
        inj.llm.malformed_json("llm.*", keep=1.0)


def test_slow_first_token_and_stalled_stream() -> None:
    inj = FaultInjector()
    llm = fake_llm(["x"], injector=inj)
    inj.llm.slow_first_token("llm.*", delay=7, times=1)
    with fake_clock() as clock:
        llm.complete()
    assert clock.sleeps == [7.0]
    inj.llm.stalled_stream("llm.*", after_chunks=2, stall=45, times=1)
    with fake_clock() as clock:
        assert "".join(c.text for c in llm.stream()) == "The answer is forty-two."
    assert clock.sleeps == [45.0]
    inj.llm.stalled_stream("llm.*", after_chunks=1, stall=30, fail=True, times=1)
    got = []
    with fake_clock() as clock, pytest.raises(APITimeoutError):
        for c in llm.stream():
            got.append(c.text)
    assert got == ["The"] and clock.total_slept == 30.0


def test_stalled_stream_async_and_stream_of_response_fault() -> None:
    inj = FaultInjector()
    llm = fake_llm(["x"], injector=inj)
    inj.llm.stalled_stream("llm.*", stall=9, times=1)
    inj.llm.refusal("llm.*", message="nope bad", times=1)

    async def run() -> list[str]:
        out = [c.text async for c in await llm.astream()]
        out += [c.text async for c in await llm.astream()]
        return out

    with fake_clock() as clock:
        out = asyncio.run(run())
    assert out[-2:] == ["nope ", "bad"] and clock.sleeps == [9.0]


def test_rate_and_seed_are_reproducible() -> None:
    def run() -> list[bool]:
        inj = FaultInjector(seed=3)
        llm = fake_llm(default="ok", injector=inj)
        inj.llm.overloaded("llm.*", rate=0.5)
        out = []
        for _ in range(12):
            try:
                llm.complete()
                out.append(True)
            except OverloadedError:
                out.append(False)
        return out

    first = run()
    assert first == run() and True in first and False in first


def test_env_loading_of_llm_rules() -> None:
    inj = FaultInjector()
    llm = fake_llm(["ok"], injector=inj)
    rules = inj.enable_from_env(
        environ={
            "PW_CHAOS": json.dumps(
                [
                    {"name": "llm.*", "llm": "rate_limit", "retry_after": 4, "times": 1},
                    {"name": "llm.*", "llm": "refusal", "message": "no", "times": 1},
                ]
            )
        }
    )
    assert len(rules) == 2
    with pytest.raises(RateLimitError) as e:
        llm.complete()
    assert e.value.retry_after == 4
    assert llm.complete().refusal == "no"
    assert llm.complete().text == "ok"


@pytest.mark.parametrize(
    "rule",
    [
        {"name": "x", "llm": "nope"},
        {"name": "x", "llm": "rate_limit", "bogus": 1},
        {"name": "x", "llm": 5},
        {"llm": "refusal"},
        {"name": "x", "llm": "server_error", "status": 404},
    ],
)
def test_env_rejects_bad_llm_rules(rule: dict[str, object]) -> None:
    inj = FaultInjector()
    with pytest.raises(ValueError):
        inj.enable_from_env(environ={"PW_CHAOS": json.dumps([rule])})
    assert inj.active() == []


def test_millisecond_agent_test() -> None:
    """FakeProvider + chaos + fake_clock: a 10-minute backoff story runs instantly."""
    import time

    llm = fake_llm(["final answer"], usage=(12, 4))

    @retry(max_attempts=5, delay=60.0, backoff=2, on=(RateLimitError, OverloadedError))
    def agent() -> str:
        return llm.complete(messages=MSG).text

    started = time.perf_counter()
    with fake_clock() as clock:
        chaos.llm.rate_limit("llm.*", times=2)
        chaos.llm.overloaded("llm.*", times=1)
        assert agent() == "final answer"
    assert clock.sleeps == [60.0, 120.0, 240.0]
    assert time.perf_counter() - started < 1.0
    assert llm.call_count == 4 and llm.input_tokens == 12
