"""LLM wrappers: classified retry, budgets, token limits, fallbacks, streams, cache."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any

import pytest

from python_wrap import CircuitBreaker, policy, retry, timeout
from python_wrap.circuit import CircuitState
from python_wrap.doctor import doctor, explain
from python_wrap.errors import CircuitOpenError, RateLimitExceeded, TimeoutExpired
from python_wrap.events import listening
from python_wrap.llm import (
    Budget,
    BudgetExceeded,
    RedisBudgetBackend,
    Spend,
    StreamTimeout,
    Usage,
    budget,
    estimate_tokens,
    fallback_models,
    llm_cache,
    llm_retry,
    llm_retry_after,
    llm_transient,
    read_usage,
    stream_timeout,
    token_rate_limit,
)
from python_wrap.presets import llm_client
from python_wrap.testing import disable_caching, disable_rate_limits


class StatusError(Exception):
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


class Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers = {"retry-after": "7"}


class WrappedError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("wrapped")
        self.response = Response(status_code)


class RateLimitError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class ContentFilterError(Exception):
    pass


class BadRequest(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.status_code = 400
        self.body = {"error": {"code": code}}


class Body(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.body = {"type": "error", "error": {"type": kind}}


def reply(inp: int = 10, out: int = 5, model: str | None = "m-1") -> dict[str, Any]:
    return {"model": model, "usage": {"input_tokens": inp, "output_tokens": out}}


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    "exc",
    [
        StatusError(429),
        StatusError(529),
        StatusError(500),
        StatusError(503),
        StatusError(408),
        StatusError(409),
        WrappedError(502),
        RateLimitError(),
        APIConnectionError(),
        Body("overloaded_error"),
        ConnectionResetError(),
        TimeoutError(),
        TimeoutExpired("t", seconds=1),
    ],
)
def test_transient_errors_are_retryable(exc: BaseException) -> None:
    assert llm_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        StatusError(400),
        StatusError(401),
        StatusError(403),
        StatusError(404),
        StatusError(422),
        WrappedError(400),
        ContentFilterError(),
        BadRequest("content_policy_violation"),
        Body("invalid_request_error"),
        ValueError("bug"),
        CircuitOpenError("open", retry_after=1),
        BudgetExceeded(
            "x", budget="b", scope="total", tokens=1, usd=0, limit_tokens=1, limit_usd=None
        ),
    ],
)
def test_other_errors_are_not_retryable(exc: BaseException) -> None:
    assert not llm_transient(exc)


def test_refusal_wins_over_a_retryable_status() -> None:
    err = StatusError(500)
    err.body = {"error": {"code": "content_filter"}}  # type: ignore[attr-defined]
    assert not llm_transient(err)


def test_retry_after_sources() -> None:
    assert llm_retry_after(StatusError(429, {"retry-after-ms": "1500"})) == 1.5
    assert llm_retry_after(StatusError(429, {"Retry-After": "3"})) == 3.0
    assert llm_retry_after(WrappedError(429)) == 7.0
    assert llm_retry_after(StatusError(429)) is None


# ------------------------------------------------------------------------- retry


def test_llm_retry_retries_transient_and_honours_retry_after(fake_clock: Any) -> None:
    calls = []

    @llm_retry(max_attempts=3, jitter=0)
    def complete() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise StatusError(429, {"retry-after": "5"})
        if len(calls) == 2:
            raise StatusError(529)
        return "ok"

    assert complete() == "ok"
    assert fake_clock.sleeps == [5.0, 2.0]  # server hint, then backoff (1 s doubled)


@pytest.mark.parametrize("exc", [StatusError(400), ContentFilterError(), ValueError()])
def test_llm_retry_never_retries_other_errors(fake_clock: Any, exc: Exception) -> None:
    calls = []

    @llm_retry()
    def complete() -> str:
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        complete()
    assert calls == [1]


def test_llm_retry_caps_server_hint(fake_clock: Any) -> None:
    calls = []

    @llm_retry(max_attempts=2, max_delay=10)
    def complete() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise StatusError(429, {"retry-after": "3600"})
        return "ok"

    assert complete() == "ok"
    assert fake_clock.sleeps == [10.0]


async def test_llm_retry_async(fake_clock: Any) -> None:
    calls = []

    @llm_retry(max_attempts=3, retry_if=lambda exc: getattr(exc, "status_code", 0) != 503)
    async def complete() -> str:
        calls.append(1)
        raise StatusError(503)

    with pytest.raises(StatusError):
        await complete()
    assert calls == [1]  # the extra predicate vetoed the retry


def test_llm_client_preset_layers_and_breaker_ignores_refusals(fake_clock: Any) -> None:
    chat = llm_client("chat", attempts=2)
    assert [layer.role for layer in chat.explain().layers] == ["breaker", "retry", "timeout"]
    calls = []

    @chat
    def complete(fail: Exception | None) -> str:
        calls.append(1)
        if fail is not None:
            raise fail
        return "ok"

    for _ in range(10):  # refusals pass straight through and never open the breaker
        with pytest.raises(ContentFilterError):
            complete(ContentFilterError())
    assert complete(None) == "ok"
    breaker = chat.explain().layers[0]
    assert breaker.settings["name"] == "chat"
    for _ in range(5):
        with pytest.raises(StatusError):
            complete(StatusError(503))
    with pytest.raises(CircuitOpenError):
        complete(None)


def test_llm_client_with_spend_has_no_findings() -> None:
    chat = llm_client("chat", spend=Budget(tokens=10), tokens=token_rate_limit(1000))
    assert chat.explain().findings == ()
    assert [layer.role for layer in chat.explain().layers][:2] == ["limiter", "limiter"]
    assert "retry" not in [
        layer.role for layer in llm_client("x", idempotent=False).explain().layers
    ]


# ------------------------------------------------------------------------- usage


def test_read_usage_shapes() -> None:
    class Obj:
        model = "m-2"

        class usage:
            prompt_tokens = 7
            completion_tokens = 3

    assert read_usage(Obj()) == Usage(7, 3, "m-2")
    assert read_usage(reply(1, 2)) == Usage(1, 2, "m-1")
    cached = {"usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 5}}
    assert read_usage(cached) == Usage(6, 2)
    assert read_usage({"text": "no usage"}) is None
    assert read_usage("plain") is None


# ------------------------------------------------------------------------ budget


def test_budget_refuses_once_exhausted_and_records_actual() -> None:
    spend = Budget(tokens=30)
    calls = []

    @spend
    def complete() -> dict[str, Any]:
        calls.append(1)
        return reply(10, 10)

    complete()
    complete()  # 40 > 30: crosses, still returned
    with pytest.raises(BudgetExceeded) as info:
        complete()
    assert len(calls) == 2
    assert info.value.tokens == 40 and info.value.limit_tokens == 30
    assert spend.spent() == Spend(40, 0.0)
    assert spend.exhausted()
    spend.reset()
    complete()


def test_budget_prices_and_usd_limit() -> None:
    spend = Budget(usd=1.0, prices={"m-": (1.0, 2.0), "m-big": (10.0, 20.0)})

    @spend
    def complete(model: str) -> dict[str, Any]:
        return reply(100_000, 50_000, model)

    complete("m-1")  # longest prefix "m-": 0.1 + 0.1
    assert spend.spent().usd == pytest.approx(0.2)
    complete("m-big-2")  # "m-big" wins: 1 + 1
    assert spend.spent().usd == pytest.approx(2.2)
    with pytest.raises(BudgetExceeded):
        complete("m-1")


def test_budget_unknown_price_raises_lookup_error() -> None:
    spend = Budget(usd=1.0, prices={"known": (1.0, 1.0)})
    with pytest.raises(LookupError):
        spend(lambda: reply(model="other"))()
    star = Budget(usd=1.0, prices={"*": (1.0, 1.0)})
    star(lambda: reply(model="other"))()
    assert star.spent().usd > 0


def test_budget_per_key() -> None:
    spend = budget(tokens=15, per="key", key=lambda user: user)

    @spend
    def ask(user: str) -> dict[str, Any]:
        return reply(10, 5)

    ask("ann")
    ask("bob")
    with pytest.raises(BudgetExceeded):
        ask("ann")
    assert spend.spent(key="bob").tokens == 15
    with pytest.raises(ValueError):
        spend.spent()


def test_budget_run_scope_and_threads() -> None:
    spend = Budget(tokens=100, per="run")
    complete = spend(lambda: reply(10, 10))
    with pytest.raises(RuntimeError):
        complete()
    with spend.run() as run:
        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(complete,))
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        complete()
        assert run.spent.tokens == 80  # threads given the context share the run
    with spend.run() as second:
        complete()
        assert second.spent.tokens == 20  # each run starts fresh


def test_budget_chain_scope() -> None:
    spend = Budget(tokens=50, per="chain")

    @spend
    def complete() -> dict[str, Any]:
        return reply(20, 10)

    @spend
    def agent(steps: int) -> str:
        for _ in range(steps):
            complete()
        return "done"

    with pytest.raises(BudgetExceeded):
        agent(3)  # 30, 60, refused
    assert agent(1) == "done"  # a new chain gets a fresh allowance
    complete()
    complete()  # each outermost call is its own chain


async def test_budget_async_and_events() -> None:
    spend = Budget(tokens=5, name="tiny")

    @spend
    async def complete() -> dict[str, Any]:
        return reply(5, 1)

    await complete()
    events: list[Any] = []
    with listening(events.append), pytest.raises(BudgetExceeded):
        await complete()
    assert events[-1].name == "llm.budget_exceeded"
    assert events[-1].fields["budget"] == "tiny"


async def test_budget_async_run_scope() -> None:
    spend = Budget(tokens=100, per="run")
    complete = spend(_async_reply)
    async with spend.run() as run:
        await asyncio.gather(complete(), complete())
        assert run.spent.tokens == 30


async def _async_reply() -> dict[str, Any]:
    return reply(10, 5)


def test_budget_period_resets(fake_clock: Any) -> None:
    spend = Budget(tokens=10, period=60)
    complete = spend(lambda: reply(10, 0))
    complete()
    with pytest.raises(BudgetExceeded):
        complete()
    fake_clock.advance(60)
    complete()


def test_budget_record_and_custom_usage() -> None:
    spend = Budget(tokens=100, usage=lambda result: (result["in"], result["out"]))
    spend(lambda: {"in": 3, "out": 4})()
    assert spend.spent().tokens == 7
    assert spend.record({"in": 1, "out": 1}).tokens == 9
    plain = Budget(tokens=100)
    assert plain.record(Usage(2, 3)).tokens == 5
    assert plain.record((1, 1)).tokens == 7


def test_budget_errors_spend_nothing() -> None:
    spend = Budget(tokens=10)

    @spend
    def complete() -> dict[str, Any]:
        raise StatusError(500)

    with pytest.raises(StatusError):
        complete()
    assert spend.spent() == Spend()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"tokens": 0},
        {"tokens": True},
        {"usd": -1, "prices": {"*": (1, 1)}},
        {"usd": 1},
        {"tokens": 1, "per": "user"},
        {"tokens": 1, "per": "key"},
        {"tokens": 1, "period": 0},
    ],
)
def test_budget_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Budget(**kwargs)


class FakeRedis:
    """Enough of a Redis client for RedisBudgetBackend."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}

    def eval(self, script: str, numkeys: int, key: str, tokens: int, usd: str, ttl: int) -> Any:
        entry = self.hashes.setdefault(key, {"tokens": 0, "usd": 0.0})
        entry["tokens"] += int(tokens)
        entry["usd"] += float(usd)
        if ttl and key not in self.ttls:
            self.ttls[key] = ttl
        return [int(entry["tokens"]), str(entry["usd"])]

    def hmget(self, key: str, *fields: str) -> list[Any]:
        entry = self.hashes.get(key)
        return [None if entry is None else entry[f] for f in fields]

    def delete(self, key: str) -> None:
        self.hashes.pop(key, None)


def test_shared_backend_is_used_by_two_budgets() -> None:
    client = FakeRedis()
    first = Budget(tokens=25, backend=RedisBudgetBackend(client), name="team", period=3600)
    second = Budget(tokens=25, backend=RedisBudgetBackend(client), name="team", period=3600)
    first(lambda: reply(10, 10))()
    second(lambda: reply(5, 5))()
    with pytest.raises(BudgetExceeded):
        first(lambda: reply())()
    assert list(client.ttls.values()) == [3_600_000]
    assert explain(policy(limiter=first)).layers[0].settings["shared"] is True


async def test_shared_backend_async() -> None:
    spend = Budget(tokens=10, backend=RedisBudgetBackend(FakeRedis()))
    await spend(_async_reply)()
    assert spend.spent().tokens == 15


# ------------------------------------------------------------- token rate limit


def test_estimate_tokens() -> None:
    messages = [{"role": "user", "content": "x" * 40}, {"content": [{"text": "y" * 40}]}]
    assert estimate_tokens(messages=messages, max_tokens=100) == 120
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens() == 1


def test_token_limit_reserves_and_reconciles(fake_clock: Any) -> None:
    limit = token_rate_limit(1000, estimate=600, block=False)
    sizes = iter([(50, 50), (300, 300), (1, 1)])
    complete = limit(lambda: reply(*next(sizes)))
    complete()
    assert limit.available == pytest.approx(900)  # 600 reserved, 100 actually used
    complete()
    assert limit.available == pytest.approx(300)
    with pytest.raises(RateLimitExceeded) as info:
        complete()
    assert info.value.retry_after == pytest.approx(300 / (1000 / 60))
    fake_clock.advance(18)  # 300 tokens refill
    complete()


def test_token_limit_blocks_and_refunds_failures(fake_clock: Any) -> None:
    limit = token_rate_limit(600, estimate=600)
    events: list[Any] = []

    @limit
    def complete(fail: bool) -> dict[str, Any]:
        if fail:
            raise StatusError(500)
        return {"no": "usage"}  # the estimate stands

    with pytest.raises(StatusError):
        complete(True)
    assert limit.available == pytest.approx(600)  # refunded
    complete(False)
    with listening(events.append):
        complete(False)
    assert fake_clock.sleeps == [pytest.approx(60)]
    assert events[0].name == "rate_limit.waited"


def test_token_limit_max_wait_and_disable(fake_clock: Any) -> None:
    limit = token_rate_limit(100, estimate=100, max_wait=1)
    complete = limit(lambda: None)
    complete()
    with pytest.raises(RateLimitExceeded):
        complete()
    with disable_rate_limits():
        complete()
    huge = token_rate_limit(10, estimate=1000, block=False)
    huge(lambda: None)()  # larger than the bucket: admitted when the bucket is full
    with pytest.raises(RateLimitExceeded):
        huge(lambda: None)()


async def test_token_limit_async(fake_clock: Any) -> None:
    limit = token_rate_limit(60, estimate=60)
    complete = limit(_async_reply)
    await complete()  # settles to the real 15
    await complete()
    await complete()
    assert fake_clock.sleeps  # the third call had to wait


# ------------------------------------------------------------------- fallbacks


def _named(label: str, fail: BaseException | None = None) -> Any:
    def call(prompt: str) -> str:
        if fail is not None:
            raise fail
        return f"{label}:{prompt}"

    call.model = label  # type: ignore[attr-defined]
    return call


def test_fallback_chain_order_and_events() -> None:
    ask = fallback_models(
        [_named("a", StatusError(529)), _named("b", RateLimitError()), _named("c")]
    )
    events: list[Any] = []
    with listening(events.append):
        assert ask("hi") == "c:hi"
    served = [e for e in events if e.name == "llm.fallback_served"]
    assert served[0].fields == {"model": "c", "index": 2, "failed": ("a", "b")}


def test_fallback_does_not_hide_refusals_or_bugs() -> None:
    ask = fallback_models([_named("a", ContentFilterError()), _named("b")])
    with pytest.raises(ContentFilterError):
        ask("hi")
    custom = fallback_models([_named("a", ValueError()), _named("b")], on=ValueError)
    assert custom("x") == "b:x"
    predicate = fallback_models([_named("a", KeyError()), _named("b")], on=lambda e: True)
    assert predicate("x") == "b:x"


def test_fallback_breaker_skips_dead_provider() -> None:
    calls = []

    def flaky(prompt: str) -> str:
        calls.append(prompt)
        raise StatusError(503)

    ask = fallback_models([flaky, _named("b")], names=["flaky", "b"])
    for _ in range(8):
        assert ask("q") == "b:q"
    assert len(calls) == 5  # the breaker opened after 5 failures


def test_fallback_custom_breakers_and_none() -> None:
    shared = CircuitBreaker(failure_threshold=1, name="provider")
    ask = fallback_models([_named("a", StatusError(500)), _named("b")], breaker={"a": shared})
    assert ask("x") == "b:x"
    assert shared.state is CircuitState.OPEN
    plain = fallback_models([_named("a", StatusError(500)), _named("b")], breaker=False)
    assert plain("x") == "b:x"


def test_fallback_cached_answer() -> None:
    state = {"down": False}

    def primary(prompt: str) -> str:
        if state["down"]:
            raise StatusError(500)
        return f"fresh:{prompt}"

    ask = fallback_models([primary], cached_answer=True, breaker=False)
    assert ask("q") == "fresh:q"
    state["down"] = True
    events: list[Any] = []
    with listening(events.append):
        assert ask("q") == "fresh:q"
    assert events[-1].fields["model"] == "cached"
    with pytest.raises(StatusError):
        ask("other")  # nothing cached for these arguments


def test_fallback_decorator_form_and_doctor() -> None:
    @fallback_models(backups=[_named("backup")], names=["primary", "backup"])
    @llm_retry(max_attempts=2)
    def primary(prompt: str) -> str:
        raise StatusError(400)

    with pytest.raises(StatusError):
        primary("x")  # 400 is not a fallback reason
    roles = [layer.role for layer in doctor(primary).layers]
    assert roles == ["fallback", "retry"]


async def test_fallback_async() -> None:
    async def down(prompt: str) -> str:
        raise APIConnectionError

    async def up(prompt: str) -> str:
        return "up"

    ask = fallback_models([down, up])
    assert await ask("x") == "up"


def test_fallback_validation() -> None:
    async def a() -> None: ...

    with pytest.raises(TypeError):
        fallback_models([a, lambda: None])
    with pytest.raises(ValueError):
        fallback_models([])
    with pytest.raises(ValueError):
        fallback_models([lambda: None], names=["a", "b"])
    with pytest.raises(ValueError):
        fallback_models(backups=[])


# ---------------------------------------------------------------- stream timeout


def _stalling(delays: list[float]) -> Any:
    for index, delay in enumerate(delays):
        time.sleep(delay)
        yield index


def test_stream_passes_through_when_fast() -> None:
    assert list(stream_timeout(iter([1, 2, 3]), first_token=1, chunk_gap=1, total=5)) == [1, 2, 3]


@pytest.mark.parametrize(
    ("delays", "limits", "tripped", "received"),
    [
        ([0.5], {"first_token": 0.05}, "first_token", []),
        ([0, 0, 0.5], {"first_token": 1, "chunk_gap": 0.05}, "chunk_gap", [0, 1]),
        ([0, 0.04, 0.04, 0.04, 0.04], {"chunk_gap": 1, "total": 0.1}, "total", [0, 1]),
    ],
)
def test_sync_stream_limits(
    delays: list[float], limits: dict[str, float], tripped: str, received: list[int]
) -> None:
    got = []
    with pytest.raises(StreamTimeout) as info:
        for item in stream_timeout(_stalling(delays), **limits):
            got.append(item)
    assert info.value.limit == tripped
    assert isinstance(info.value, TimeoutError)
    assert got[: len(received)] == received


def test_sync_stream_propagates_errors_and_closes_source() -> None:
    def broken() -> Any:
        yield 1
        raise StatusError(500)

    stream = stream_timeout(broken(), chunk_gap=1)
    assert next(stream) == 1
    with pytest.raises(StatusError):
        next(stream)

    class Http:
        closed = False

        def __iter__(self) -> Any:
            time.sleep(0.3)
            yield "late"

        def close(self) -> None:
            self.closed = True

        def final_message(self) -> str:
            return "helper"

    source = Http()
    guarded = stream_timeout(source, first_token=0.05)
    assert guarded.final_message() == "helper"  # type: ignore[attr-defined]
    with pytest.raises(StreamTimeout):
        next(guarded)
    assert source.closed


def test_stream_decorator_counts_from_the_call() -> None:
    @stream_timeout(first_token=0.05, chunk_gap=1)
    def generate() -> Any:
        time.sleep(0.2)
        yield "x"

    events: list[Any] = []
    with listening(events.append), pytest.raises(StreamTimeout):
        list(generate())
    assert events[-1].name == "timeout.expired"


async def test_async_stream_limits() -> None:
    async def chunks(delays: list[float]) -> Any:
        for index, delay in enumerate(delays):
            await asyncio.sleep(delay)
            yield index

    got = [item async for item in stream_timeout(chunks([0, 0]), chunk_gap=1)]
    assert got == [0, 1]
    with pytest.raises(StreamTimeout) as info:
        async for item in stream_timeout(chunks([0, 0.5]), first_token=1, chunk_gap=0.05):
            got.append(item)
    assert info.value.limit == "chunk_gap"

    @stream_timeout(first_token=0.05)
    async def slow_open() -> Any:
        await asyncio.sleep(0.5)
        return chunks([0])

    with pytest.raises(StreamTimeout) as info:
        await slow_open()
    assert info.value.limit == "first_token"

    @stream_timeout(chunk_gap=1)
    async def opens() -> Any:
        return chunks([0, 0])

    assert [item async for item in await opens()] == [0, 1]

    @stream_timeout(total=0.05)
    async def agen() -> Any:
        yield 1
        await asyncio.sleep(0.5)
        yield 2

    with pytest.raises(StreamTimeout):
        _ = [item async for item in agen()]


def test_stream_validation() -> None:
    with pytest.raises(ValueError):
        stream_timeout(iter([]))
    with pytest.raises(ValueError):
        stream_timeout(first_token=0)
    with pytest.raises(TypeError):
        stream_timeout(5, total=1)  # type: ignore[call-overload]


# ------------------------------------------------------------------------- cache


def test_cache_exact_match_ignores_transport_args() -> None:
    calls = []

    @llm_cache()
    def complete(*, model: str, messages: list[dict[str, str]], **params: Any) -> str:
        calls.append(params)
        return f"answer-{len(calls)}"

    msgs = [{"role": "user", "content": "hi"}]
    assert complete(model="m", messages=msgs, temperature=0) == "answer-1"
    assert complete(model="m", messages=msgs, temperature=0, timeout=3) == "answer-1"
    assert complete(model="m", messages=msgs, temperature=1) == "answer-2"
    assert complete(model="other", messages=msgs, temperature=0) == "answer-3"
    assert complete(model="m", messages=msgs, temperature=0, stream=True) == "answer-4"
    with disable_caching():
        assert complete(model="m", messages=msgs, temperature=0) == "answer-5"


def test_cache_ttl_and_failures(fake_clock: Any) -> None:
    calls = []

    @llm_cache(ttl=10)
    def complete(prompt: str) -> str | None:
        calls.append(prompt)
        if prompt == "boom":
            raise StatusError(500)
        return None if prompt == "none" else prompt.upper()

    complete("a")
    complete("a")
    assert calls == ["a"]
    fake_clock.advance(11)
    complete("a")
    assert calls == ["a", "a"]
    for _ in range(2):
        with pytest.raises(StatusError):
            complete("boom")
        complete("none")
    assert calls.count("boom") == 2 and calls.count("none") == 2


def test_cache_custom_store_and_key() -> None:
    class DictStore:
        def __init__(self) -> None:
            self.data: dict[Any, Any] = {}

        def get(self, key: Any) -> Any:
            return self.data.get(key)

        def set(self, key: Any, entry: Any, ttl: float | None) -> None:
            self.data[key] = entry

        def delete(self, key: Any) -> bool:
            return self.data.pop(key, None) is not None

        def clear(self) -> None:
            self.data.clear()

        def __len__(self) -> int:
            return len(self.data)

    store = DictStore()
    calls = []

    @llm_cache(store=store, key=lambda prompt, user: prompt)
    def complete(prompt: str, user: str) -> str:
        calls.append(user)
        return prompt

    complete("p", "ann")
    complete("p", "bob")
    assert calls == ["ann"] and len(store) == 1


def test_cache_similarity_hook() -> None:
    vectors = {"hello there": [1.0, 0.0], "hello there!": [0.99, 0.05], "bye": [0.0, 1.0]}
    calls = []

    @llm_cache(similarity=lambda text: vectors[text], threshold=0.9)
    def complete(*, model: str, prompt: str) -> str:
        calls.append(prompt)
        return f"re:{prompt}"

    events: list[Any] = []
    assert complete(model="m", prompt="hello there") == "re:hello there"
    with listening(events.append):
        assert complete(model="m", prompt="hello there!") == "re:hello there"
    assert [e.name for e in events] == ["cache.hit"]
    assert complete(model="m", prompt="bye") == "re:bye"
    assert complete(model="other", prompt="hello there!") == "re:hello there!"  # same model only
    assert calls == ["hello there", "bye", "hello there!"]


async def test_cache_async_with_async_store_and_embedding() -> None:
    from python_wrap import AsyncMemoryStore

    calls = []

    async def embed(text: str) -> list[float]:
        return [1.0, float(len(text) > 10)]

    @llm_cache(store=AsyncMemoryStore(maxsize=10), similarity=embed)
    async def complete(prompt: str) -> str:
        calls.append(prompt)
        return prompt

    assert await complete("short") == "short"
    assert await complete("short") == "short"
    assert await complete("tiny") == "short"  # same vector: semantic hit
    assert calls == ["short"]


def test_cache_rejects_async_store_on_sync_function() -> None:
    from python_wrap import AsyncMemoryStore

    with pytest.raises(TypeError):
        llm_cache(store=AsyncMemoryStore())(lambda: None)


@pytest.mark.parametrize(
    "kwargs", [{"ttl": 0}, {"maxsize": 0}, {"threshold": 0}, {"threshold": 1.5}]
)
def test_cache_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        llm_cache(**kwargs)


# ------------------------------------------------------------------------ doctor


def _codes(target: Any) -> list[str]:
    report = (
        explain(target) if not callable(getattr(target, "__wrapped__", None)) else doctor(target)
    )
    return [finding.code for finding in report.findings]


def test_doctor_pw013_retry_without_budget() -> None:
    assert _codes(llm_client("chat")) == ["PW013"]
    assert _codes(llm_client("chat", spend=Budget(tokens=1))) == []


def test_doctor_pw014_no_timeout_and_stream_without_gap() -> None:
    @Budget(tokens=10)
    def complete() -> None: ...

    assert _codes(complete) == ["PW014"]

    @Budget(tokens=10)
    @stream_timeout(first_token=5)
    def stream() -> Any:
        yield 1

    assert _codes(stream) == ["PW014"]

    @Budget(tokens=10)
    @stream_timeout(first_token=5, chunk_gap=5)
    def good() -> Any:
        yield 1

    assert _codes(good) == []


def test_doctor_pw015_generic_retry_on_llm_call() -> None:
    risky = policy(
        limiter=Budget(tokens=10),
        retry=retry(max_attempts=3, on=Exception),
        timeout=timeout(30),
    )
    assert "PW015" in _codes(risky)
    fine = policy(retry=retry(max_attempts=3, on=ConnectionError), timeout=timeout(30))
    assert "PW015" not in _codes(fine)  # no LLM layers: the LLM checks stay silent


def test_doctor_multiprocess_flags_local_llm_limits() -> None:
    report = explain(
        policy(limiter=[Budget(tokens=1), token_rate_limit(10)], timeout=timeout(1)),
        multiprocess=True,
    )
    assert [finding.code for finding in report.findings] == ["PW012", "PW012"]
