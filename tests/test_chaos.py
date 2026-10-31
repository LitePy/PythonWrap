from __future__ import annotations

import asyncio
import inspect
import random
import threading

import pytest

from python_wrap import CircuitBreaker, policy, retry
from python_wrap.chaos import FaultInjector, chaos, fault_point
from python_wrap.events import listening


@pytest.fixture(autouse=True)
def _clean() -> None:
    chaos.clear()
    chaos.enabled = True
    chaos.reseed(None)
    yield  # type: ignore[misc]
    chaos.clear()
    chaos.enabled = True


@fault_point("svc.call")
def call(x: int, y: int = 1) -> int:
    """doc"""
    return x + y


@fault_point("svc.acall")
async def acall(x: int) -> int:
    return x * 2


def test_noop_when_inactive_and_signature_preserved() -> None:
    assert call(1, y=2) == 3
    assert call.__name__ == "call" and call.__doc__ == "doc"
    assert list(inspect.signature(call).parameters) == ["x", "y"]
    assert inspect.iscoroutinefunction(acall)
    assert chaos.active() == []


def test_latency_exact_with_fake_clock(fake_clock) -> None:  # type: ignore[no-untyped-def]
    with chaos.inject("svc.call", latency=0.5):
        assert call(1) == 2
    assert fake_clock.sleeps == [0.5]


def test_latency_range_is_seeded(fake_clock) -> None:  # type: ignore[no-untyped-def]
    chaos.reseed(7)
    expected = random.Random(7).uniform(1, 2)
    with chaos.inject("svc.call", latency=(1, 2)):
        call(1)
    assert fake_clock.sleeps == [expected]


def test_exception_forms() -> None:
    inst = KeyError("boom")
    with chaos.inject("svc.call", exception=inst):
        with pytest.raises(KeyError) as ei:
            call(1)
        assert ei.value is inst
    with chaos.inject("svc.call", exception=ConnectionError), pytest.raises(ConnectionError):
        call(1)
    with (
        chaos.inject("svc.call", exception=lambda: ValueError("made")),
        pytest.raises(ValueError, match="made"),
    ):
        call(1)


def test_rate_with_seeded_rng() -> None:
    inj = FaultInjector(seed=123)
    rng = random.Random(123)
    expected = [rng.random() < 0.3 for _ in range(50)]

    @fault_point("p", injector=inj)
    def f() -> str:
        return "ok"

    inj.inject("p", result="chaos", rate=0.3)
    got = [f() == "chaos" for _ in range(50)]
    assert got == expected
    assert inj.stats("p") == sum(expected)


def test_times_limit_and_stats() -> None:
    fault = chaos.inject("svc.call", result=0, times=2)
    assert [call(1), call(1), call(1)] == [0, 0, 2]
    assert fault.injected == 2 and not fault.active
    assert chaos.stats("svc.call") == 2
    assert chaos.stats() == {"svc.call": 2}
    assert chaos.active() == []


def test_result_override_skips_call() -> None:
    calls: list[int] = []

    @fault_point("r")
    def f() -> int:
        calls.append(1)
        return 1

    with chaos.inject("r", result=None):
        assert f() is None
    assert calls == []


def test_patterns() -> None:
    @fault_point("payments.charge")
    def charge() -> str:
        return "ok"

    @fault_point("inventory")
    def inv() -> str:
        return "ok"

    with chaos.inject("payments.*", result="x"):
        assert charge() == "x"
        assert inv() == "ok"


def test_context_manager_and_remove() -> None:
    with chaos.inject("svc.call", result=9) as fault:
        assert fault.active and call(1) == 9
    assert call(1) == 2 and not fault.active
    f2 = chaos.inject("svc.call", result=9)
    f2.remove()
    f2.remove()
    assert call(1) == 2


def test_inject_validation() -> None:
    with pytest.raises(ValueError):
        chaos.inject("x")
    with pytest.raises(ValueError):
        chaos.inject("x", exception=ValueError, result=1)
    with pytest.raises(ValueError):
        chaos.inject("x", result=1, rate=2)
    with pytest.raises(ValueError):
        chaos.inject("x", result=1, times=0)
    with pytest.raises(ValueError):
        chaos.inject("x", latency=(2, 1))
    with pytest.raises(ValueError):
        chaos.inject("", result=1)


def test_kill_switch() -> None:
    chaos.inject("svc.call", exception=ValueError)
    chaos.enabled = False
    assert call(1) == 2
    chaos.enabled = True
    with pytest.raises(ValueError):
        call(1)


def test_env_parsing_valid() -> None:
    env = {
        "PW_CHAOS": '[{"name":"payments.*","latency":0.5,"rate":0.1},'
        '{"name":"inventory","exception":"ConnectionError"},'
        '{"name":"x","exception":"python_wrap.errors:CircuitOpenError","times":1},'
        '{"name":"y","result":{"a":1},"latency":[0.1,0.2]}]'
    }
    faults = chaos.enable_from_env(environ=env)
    assert len(faults) == 4
    assert faults[0].latency == 0.5 and faults[0].rate == 0.1
    assert faults[1].exception is ConnectionError
    assert faults[3].result == {"a": 1} and faults[3].latency == (0.1, 0.2)


def test_env_unset_is_noop() -> None:
    assert chaos.enable_from_env(environ={}) == []
    assert chaos.enable_from_env(environ={"PW_CHAOS": "  "}) == []
    assert chaos.active() == []


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("{nope", "not valid JSON"),
        ('{"name": "a"}', "JSON list"),
        ("[1]", "must be an object"),
        ('[{"latency": 1}]', "missing 'name'"),
        ('[{"name":"a","bogus":1}]', "unknown key"),
        ('[{"name":"a","exception":"NoSuchError"}]', "unknown exception"),
        ('[{"name":"a","exception":"nomodule_zz:X"}]', "cannot import"),
        ('[{"name":"a","exception":"str"}]', "unknown exception"),
        ('[{"name":"a","latency":1,"rate":5}]', "rate"),
        ('[{"name":"a"}]', "at least one"),
    ],
)
def test_env_invalid(raw: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        chaos.enable_from_env("MYVAR", environ={"MYVAR": raw})
    assert chaos.active() == []


def test_env_bad_rule_installs_none() -> None:
    raw = '[{"name":"a","result":1},{"name":"b","rate":9,"result":1}]'
    with pytest.raises(ValueError):
        chaos.enable_from_env(environ={"PW_CHAOS": raw})
    assert chaos.active() == []


async def test_async_latency_exception_result(fake_clock) -> None:  # type: ignore[no-untyped-def]
    with chaos.inject("svc.acall", latency=2):
        assert await acall(2) == 4
    assert fake_clock.sleeps == [2.0]
    with chaos.inject("svc.acall", exception=TimeoutError), pytest.raises(TimeoutError):
        await acall(1)
    with chaos.inject("svc.acall", result=-1):
        assert await acall(1) == -1


async def test_async_concurrent_times_limit() -> None:
    chaos.inject("svc.acall", result=0, times=3)
    results = await asyncio.gather(*(acall(1) for _ in range(10)))
    assert results.count(0) == 3


def test_open_circuit() -> None:
    breaker = CircuitBreaker(name="chaos-db")
    with chaos.open_circuit(breaker):
        assert breaker.state.value == "open"
    assert breaker.state.value == "closed"
    breaker.force_open()
    with chaos.open_circuit(breaker):
        pass
    assert breaker.state.value == "open"


def test_thread_safety_smoke() -> None:
    fault = chaos.inject("svc.call", result=0, times=100)
    out: list[int] = []

    def work() -> None:
        for _ in range(100):
            out.append(call(1))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.count(0) == 100 and fault.injected == 100
    assert chaos.stats("svc.call") == 100


def test_events() -> None:
    seen = []
    with listening(seen.append):
        with chaos.inject("svc.call", latency=0.0), chaos.inject("svc.call", result=1):
            call(1)
        with chaos.inject("svc.call", exception=ValueError), pytest.raises(ValueError):
            call(1)
    assert [(e.name, e.function, e.fields["kind"]) for e in seen] == [
        ("chaos.injected", "svc.call", "latency"),
        ("chaos.injected", "svc.call", "result"),
        ("chaos.injected", "svc.call", "exception"),
    ]


def test_policy_inner_retry_recovers(fake_clock) -> None:  # type: ignore[no-untyped-def]
    @policy(retry=retry(max_attempts=3, delay=1, on=ConnectionError), inner=fault_point("pol.x"))
    def fetch() -> str:
        return "data"

    fault = chaos.inject("pol.x", exception=ConnectionError, times=2)
    assert fetch() == "data"
    assert fault.injected == 2
    assert fake_clock.sleeps == [1.0, 1.0]


def test_policy_retry_exhausted() -> None:
    @policy(retry=retry(max_attempts=2, delay=0, on=ConnectionError), inner=fault_point("pol.y"))
    def fetch() -> str:
        return "data"

    with (
        chaos.inject("pol.y", exception=ConnectionError("down")),
        pytest.raises(ConnectionError),
    ):
        fetch()
