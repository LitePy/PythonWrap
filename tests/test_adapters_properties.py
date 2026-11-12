"""Property tests for the SDK adapters: budgets, bounded attempts, resume safety."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from python_wrap.adapters import (
    CacheGuard,
    StreamInterrupted,
    guard_anthropic,
    guard_openai,
    resume_safe,
    seed_limiter,
)
from python_wrap.llm import Budget, BudgetExceeded, llm_retry, token_rate_limit
from python_wrap.testing import fake_clock

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


class Transient(Exception):
    status_code = 503


class Endpoint:
    """A resource whose client may retry by itself, counting HTTP requests."""

    def __init__(self, client: Client, replies: list[Any]) -> None:
        self.client, self.replies, self.http = client, replies, [0]

    def create(self, **kwargs: Any) -> Any:
        for attempt in range(1 + self.client.max_retries):
            self.http[0] += 1
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if isinstance(reply, BaseException):
                if attempt < self.client.max_retries:
                    continue
                raise reply
            return reply
        raise AssertionError


class Client:
    def __init__(self, replies: list[Any], max_retries: int) -> None:
        self.max_retries = max_retries
        self.endpoint = Endpoint(self, replies)
        self.chat = SimpleNamespace(completions=self.endpoint)
        self.messages = self.endpoint

    def with_options(self, *, max_retries: int) -> Client:
        clone = Client([], max_retries)
        clone.endpoint = Endpoint(clone, self.endpoint.replies)
        clone.endpoint.http = self.endpoint.http
        clone.chat = SimpleNamespace(completions=clone.endpoint)
        clone.messages = clone.endpoint
        return clone


def reply(tokens: int) -> Any:
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=tokens, output_tokens=0))


# ------------------------------------------------------------------------ budget


@SETTINGS
@given(
    limit=st.integers(1, 2_000),
    usages=st.lists(st.integers(0, 500), min_size=1, max_size=40),
)
def test_budget_overshoot_is_at_most_one_call(limit: int, usages: list[int]) -> None:
    spend = Budget(tokens=limit)
    client = Client([reply(tokens) for tokens in usages] + [reply(0)], max_retries=2)
    guarded = guard_anthropic(client, budget=spend)
    charged: list[int] = []
    refused = False
    for tokens in usages:
        before = spend.spent().tokens
        try:
            guarded.messages.create(model="m")
        except BudgetExceeded:
            refused = True
            assert before >= limit  # refused only once the limit is reached
            break
        assert before < limit  # admitted only while under the limit
        charged.append(tokens)
    spent = spend.spent().tokens
    assert spent == sum(charged)
    assert spent < limit + max(usages)  # overshoot: at most one call's usage
    if charged:
        assert spent - charged[-1] < limit  # everything before the last call was under
    assert refused or len(charged) == len(usages)


# -------------------------------------------------------------------- attempts


@SETTINGS
@given(
    attempts=st.integers(1, 6),
    sdk_retries=st.integers(0, 4),
    nested=st.lists(st.integers(1, 5), max_size=3),
)
def test_total_attempts_are_bounded_across_layers(
    attempts: int, sdk_retries: int, nested: list[int]
) -> None:
    client = Client([Transient()], max_retries=sdk_retries)
    guarded = guard_openai(client, attempts=attempts, retry={"delay": 0, "jitter": 0})

    def call() -> Any:
        return guarded.chat.completions.create(model="m")

    for inner_attempts in nested:  # extra llm_retry layers inside the outer owner
        call = llm_retry(max_attempts=inner_attempts, delay=0, jitter=0)(call)

    @llm_retry(max_attempts=attempts, delay=0, jitter=0, owns_retries=True)
    def owner() -> Any:
        return call()

    with fake_clock(), pytest.raises(Transient):
        owner()
    assert client.endpoint.http[0] == attempts  # no layer multiplies, the SDK never retries


@SETTINGS
@given(attempts=st.integers(1, 6), sdk_retries=st.integers(0, 4))
def test_guard_alone_makes_exactly_attempts_requests(attempts: int, sdk_retries: int) -> None:
    client = Client([Transient()], max_retries=sdk_retries)
    guarded = guard_anthropic(client, attempts=attempts, retry={"delay": 0, "jitter": 0})
    with fake_clock(), pytest.raises(Transient):
        guarded.messages.create(model="m")
    assert client.endpoint.http[0] == attempts


# ---------------------------------------------------------------------- resume


class Events:
    def __init__(self, events: list[Any], fail_at: int | None) -> None:
        self.events, self.fail_at = events, fail_at

    def __iter__(self) -> Iterator[Any]:
        for index, event in enumerate(self.events):
            if index == self.fail_at:
                raise ConnectionResetError("reset")
            yield event
        if self.fail_at is not None and self.fail_at >= len(self.events):
            raise ConnectionResetError("reset")


@SETTINGS
@given(
    prelude=st.integers(0, 3),
    texts=st.lists(st.text(min_size=1, max_size=5), max_size=6),
    fail_at=st.integers(0, 10),
)
def test_resume_never_duplicates_and_never_replays_output(
    prelude: int, texts: list[str], fail_at: int
) -> None:
    events: list[Any] = [SimpleNamespace(type="message_start", message=None)] * prelude
    events += texts
    fail_at = min(fail_at, len(events))
    opened: list[int] = []

    def factory() -> Events:
        opened.append(1)
        return Events(events, fail_at if len(opened) == 1 else None)

    stream = resume_safe(factory, delay=0)
    received: list[Any] = []
    with fake_clock():
        try:
            for event in stream:
                received.append(event)
        except StreamInterrupted as exc:
            assert fail_at > prelude  # output had started
            assert received == events[:fail_at]
            assert exc.text == "".join(texts[: fail_at - prelude])
            assert len(opened) == 1  # never reopened after output
            return
    assert received == events  # a failure before output was invisible
    assert fail_at <= prelude and len(opened) == 2


# ---------------------------------------------------------------------- limiter


@SETTINGS
@given(
    capacity=st.integers(1, 100_000),
    spent=st.integers(0, 100_000),
    remaining=st.integers(0, 200_000),
)
def test_seed_limiter_never_raises_the_bucket(capacity: int, spent: int, remaining: int) -> None:
    with fake_clock():
        limiter = token_rate_limit(capacity)
        limiter.settle(0, min(spent, capacity))
        before = limiter.available
        seed_limiter(limiter, {"x-ratelimit-remaining-tokens": str(remaining)})
        after = limiter.available
    assert after <= before
    assert after <= remaining or after == before


@SETTINGS
@given(
    calls=st.lists(
        st.tuples(st.integers(0, 5_000), st.integers(0, 5_000), st.integers(0, 5_000)),
        max_size=20,
    )
)
def test_cache_hit_rate_is_a_fraction(calls: list[tuple[int, int, int]]) -> None:
    guard = CacheGuard(min_prompt_tokens=0, warn=False)
    for inp, read, write in calls:
        usage = SimpleNamespace(
            input_tokens=inp,
            output_tokens=1,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=write,
        )
        guard.observe(SimpleNamespace(usage=usage))
    stats = guard.stats()
    assert 0.0 <= stats.hit_rate <= 1.0
    assert stats.cache_read_tokens == sum(read for _, read, _ in calls)
