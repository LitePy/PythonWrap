from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.config import configure, configured, get_config, reset_config
from python_wrap.deadline import deadline
from python_wrap.logs import audit, log_args
from python_wrap.retry import retry, retry_stats, retrying


@pytest.fixture(autouse=True)
def clean_config() -> Iterator[None]:
    reset_config()
    try:
        yield
    finally:
        reset_config()


class Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError(f"fail {self.calls}")
        return "ok"


def test_defaults_and_get_config_is_a_copy() -> None:
    cfg = get_config()
    assert cfg == {"logger": None, "retry": {}, "redact": None, "redact_extra": ()}
    cfg["retry"]["delay"] = 5
    assert get_config()["retry"] == {}


def test_unknown_key_has_hint() -> None:
    with pytest.raises(TypeError, match="Did you mean 'retry'"):
        configure(retyr={})
    with pytest.raises(TypeError, match="Did you mean 'max_attempts'"):
        configure(retry={"max_attemps": 3})
    with pytest.raises(TypeError, match="Unknown configuration key 'zzz'"):
        configure(zzz=1)


def test_validation() -> None:
    with pytest.raises(ValueError):
        configure(retry={"max_attempts": 0})
    with pytest.raises(ValueError):
        configure(retry={"backoff": 0.5})
    with pytest.raises(TypeError):
        configure(retry=[1])
    with pytest.raises(TypeError):
        configure(logger=3)
    with pytest.raises(TypeError):
        configure(redact="password")
    with pytest.raises(TypeError):
        configure(redact_extra=[1])
    assert get_config()["retry"] == {}  # failed calls change nothing


def test_retry_defaults_apply_and_explicit_wins(fake_clock: FakeClock) -> None:
    configure(retry={"max_attempts": 5, "delay": 0.5, "backoff": 2.0})
    f = Flaky(3)
    assert retry(f)() == "ok"
    assert fake_clock.sleeps == [0.5, 1.0, 2.0]

    g = Flaky(10)
    with pytest.raises(ValueError):
        retry(g, max_attempts=2)()
    assert g.calls == 2


def test_retry_defaults_max_delay_max_time_log_level(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    configure(
        retry={
            "max_attempts": None,
            "delay": 1,
            "backoff": 10,
            "max_delay": 3,
            "jitter": 0.0,
            "max_time": 7,
            "log_level": logging.ERROR,
        }
    )
    f = Flaky(100)
    with caplog.at_level(logging.ERROR, logger="python_wrap"), pytest.raises(ValueError):
        retry(f)()
    assert fake_clock.sleeps == [1, 3, 3]
    assert caplog.records
    assert all(r.levelno == logging.ERROR for r in caplog.records)


def test_retry_default_jitter(fake_clock: FakeClock) -> None:
    configure(retry={"delay": 1, "jitter": 0.5})
    retry(Flaky(1))()
    assert 1 <= fake_clock.sleeps[0] <= 1.5


def test_retry_default_none_disables_log(caplog: pytest.LogCaptureFixture) -> None:
    configure(retry={"delay": 0, "log_level": None})
    with caplog.at_level(logging.DEBUG, logger="python_wrap"):
        assert retry(Flaky(1))() == "ok"
    assert not caplog.records


def test_retrying_uses_defaults(fake_clock: FakeClock) -> None:
    configure(retry={"max_attempts": 2, "delay": 4})
    f = Flaky(10)
    with pytest.raises(ValueError):
        for attempt in retrying():
            with attempt:
                f()
    assert f.calls == 2
    assert fake_clock.sleeps == [4]


def test_decorators_applied_earlier_keep_their_settings(fake_clock: FakeClock) -> None:
    early = retry(Flaky(10))
    configure(retry={"max_attempts": 7})
    late = retry(Flaky(10))
    with pytest.raises(ValueError):
        early()
    with pytest.raises(ValueError):
        late()
    stats = retry_stats(early)
    assert stats is not None and stats.attempts_total == 3
    stats = retry_stats(late)
    assert stats is not None and stats.attempts_total == 7


def test_logger_default(caplog: pytest.LogCaptureFixture) -> None:
    mine = logging.getLogger("my.app")
    configure(logger=mine)
    assert _core.get_logger(None) is mine
    assert _core.get_logger("other").name == "other"

    @log_args(level=logging.INFO)
    def f(x: int) -> int:
        return x

    with caplog.at_level(logging.INFO, logger="my.app"):
        f(1)
    assert [r.name for r in caplog.records] == ["my.app"]

    configure(logger="by.name")
    assert _core.get_logger(None).name == "by.name"
    configure(logger=None)
    assert _core.get_logger(None).name == _core.LOGGER_NAME


def test_configured_restores() -> None:
    mine = logging.getLogger("my.app")
    configure(retry={"delay": 9})
    with configured(logger=mine, retry={"max_attempts": 2}, redact_extra=["ssn"]):
        cfg = get_config()
        assert cfg["retry"] == {"max_attempts": 2}
        assert cfg["redact_extra"] == ("ssn",)
        assert _core.get_logger(None) is mine
    assert get_config()["retry"] == {"delay": 9}
    assert get_config()["redact_extra"] == ()
    assert _core.get_logger(None).name == _core.LOGGER_NAME


def test_configured_restores_on_error() -> None:
    with pytest.raises(RuntimeError), configured(retry={"max_attempts": 2}):
        raise RuntimeError
    assert get_config()["retry"] == {}


def test_configure_merges_keys_but_replaces_values() -> None:
    configure(retry={"delay": 1}, redact_extra=["a"])
    configure(retry={"jitter": 0.1})
    cfg = get_config()
    assert cfg["retry"] == {"jitter": 0.1}
    assert cfg["redact_extra"] == ("a",)


def test_redact_config_for_log_args_and_audit(caplog: pytest.LogCaptureFixture) -> None:
    configure(redact=["ssn"])

    @log_args(level=logging.INFO)
    def f(ssn: str, password: str) -> None: ...

    with caplog.at_level(logging.INFO, logger="python_wrap"):
        f("123", "pw")
    msg = caplog.records[-1].getMessage()
    assert "ssn='***'" in msg and "password='pw'" in msg  # configured list replaced defaults

    records: list[object] = []

    @audit(sink=records.append)
    def g(ssn: str, password: str) -> None: ...

    g("123", "pw")
    text = repr(records[0])
    assert "123" not in text and "pw" in text


def test_redact_extra_adds_to_defaults_everywhere(caplog: pytest.LogCaptureFixture) -> None:
    @log_args(level=logging.INFO, redact_extra=["ssn"])
    def f(ssn: str, password: str, name: str) -> None: ...

    with caplog.at_level(logging.INFO, logger="python_wrap"):
        f("123", "pw", "bob")
    msg = caplog.records[-1].getMessage()
    assert "ssn='***'" in msg and "password='***'" in msg and "name='bob'" in msg

    configure(redact_extra=["name"])
    records: list[object] = []

    @audit(redact_extra=["ssn"], sink=records.append)
    def g(ssn: str, token: str, name: str, other: str) -> None: ...

    g("1", "s3cr3t", "bob", "keep")
    text = repr(records[0])
    assert "bob" not in text and "s3cr3t" not in text and "'1'" not in text
    assert "keep" in text


def test_redact_extra_with_explicit_redact(caplog: pytest.LogCaptureFixture) -> None:
    @log_args(level=logging.INFO, redact=["xx"], redact_extra=["yy"])
    def f(xx: int, yy: int, password: str) -> None: ...

    with caplog.at_level(logging.INFO, logger="python_wrap"):
        f(1, 2, "pw")
    msg = caplog.records[-1].getMessage()
    assert "xx='***'" in msg and "yy='***'" in msg and "password='pw'" in msg


def test_retry_respects_deadline(fake_clock: FakeClock) -> None:
    f = Flaky(100)
    with deadline(5), pytest.raises(ValueError):
        retry(f, max_attempts=None, delay=2)()
    # sleeps 2, 2 (t=4); the next 2s would run past the 5s deadline
    assert fake_clock.sleeps == [2, 2]
    assert f.calls == 3


def test_retry_deadline_already_expired(fake_clock: FakeClock) -> None:
    f = Flaky(100)
    with deadline(0), pytest.raises(ValueError):
        retry(f, delay=0.1)()
    assert fake_clock.sleeps == []
    assert f.calls == 1


def test_retry_without_deadline_unchanged(fake_clock: FakeClock) -> None:
    f = Flaky(2)
    assert retry(f, delay=100)() == "ok"
    assert fake_clock.sleeps == [100, 100]


def test_async_retry_respects_deadline(fake_clock: FakeClock) -> None:
    import asyncio

    f = Flaky(100)

    @retry(max_attempts=None, delay=2)
    async def call() -> str:
        return f()

    async def run() -> None:
        with deadline(3):
            await call()

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert fake_clock.sleeps == [2]


def test_retrying_block_respects_deadline(fake_clock: FakeClock) -> None:
    f = Flaky(100)
    with deadline(1), pytest.raises(ValueError):
        for attempt in retrying(max_attempts=None, delay=2):
            with attempt:
                f()
    assert f.calls == 1
