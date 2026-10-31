"""Tests for webhook circuit alerts."""

from __future__ import annotations

import json
import logging
import threading

import pytest

from python_wrap import CircuitBreaker
from python_wrap.alerts import WebhookAlerter
from python_wrap.circuit import CircuitState

URL = "https://chat.example.com/hooks/SECRET-TOKEN"


class Recorder:
    def __init__(self):
        self.calls = []
        self.threads = []

    def __call__(self, url, body, headers):
        self.threads.append(threading.current_thread())
        self.calls.append((url, json.loads(body), dict(headers)))

    @property
    def bodies(self):
        return [c[1] for c in self.calls]


def make(rec, **kw):
    return WebhookAlerter(URL, sender=rec, **kw)


def trip(alerter, name="payments"):
    breaker = CircuitBreaker(failure_threshold=1, name=name, on_state_change=alerter)
    breaker.force_open()
    return breaker


def test_json_payload_shape():
    rec = Recorder()
    alerter = make(rec, context={"service": "billing"})
    trip(alerter)
    assert alerter.flush(2)
    body = rec.bodies[0]
    assert body["breaker"] == "payments"
    assert body["state"] == "open" and body["previous_state"] == "closed"
    assert body["context"] == {"service": "billing"}
    assert body["kind"] == "alert" and body["time"].endswith("Z")
    assert rec.calls[0][0] == URL
    assert rec.calls[0][2]["Content-Type"] == "application/json"


def test_slack_payload_shape():
    rec = Recorder()
    alerter = make(rec, format="slack", context={"env": "prod"})
    trip(alerter)
    alerter.flush(2)
    (body,) = rec.bodies
    assert set(body) == {"text"}
    assert "payments" in body["text"] and "OPEN" in body["text"] and "env: prod" in body["text"]


def test_teams_payload_is_adaptive_card():
    rec = Recorder()
    alerter = make(rec, format="teams", context={"service": "billing", "env": "prod"})
    trip(alerter)
    alerter.flush(2)
    (body,) = rec.bodies
    assert body["type"] == "message"
    (attachment,) = body["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["Breaker"] == "payments" and facts["State"] == "open"
    assert facts["service"] == "billing" and facts["env"] == "prod" and "Time" in facts


def test_cooldown_dedupes_per_breaker(fake_clock):
    rec = Recorder()
    alerter = make(rec, cooldown=300)
    assert alerter.notify("a", "closed", "open")
    assert not alerter.notify("a", "half_open", "open")
    assert alerter.notify("b", "closed", "open")
    fake_clock.advance(299)
    assert not alerter.notify("a", "closed", "open")
    fake_clock.advance(2)
    assert alerter.notify("a", "closed", "open")
    alerter.flush(2)
    assert len(rec.calls) == 3


def test_only_configured_states_alert():
    rec = Recorder()
    alerter = make(rec, on=(CircuitState.HALF_OPEN,))
    assert not alerter.notify("a", "closed", "open")
    assert alerter.notify("a", "open", "half_open")


def test_recovery_message_only_after_alert():
    rec = Recorder()
    alerter = make(rec)
    assert not alerter.notify("a", "half_open", "closed")
    alerter.notify("a", "closed", "open")
    assert alerter.notify("a", "half_open", "closed")
    assert not alerter.notify("a", "half_open", "closed")
    alerter.flush(2)
    kinds = sorted(b["kind"] for b in rec.bodies)
    assert kinds == ["alert", "recovery"]
    assert any("closed again" in b["text"] for b in rec.bodies)


def test_recovery_can_be_disabled():
    rec = Recorder()
    alerter = make(rec, include_recovery=False)
    alerter.notify("a", "closed", "open")
    assert not alerter.notify("a", "half_open", "closed")


def test_sender_runs_on_background_thread_and_does_not_block():
    gate = threading.Event()
    threads = []

    def slow(url, body, headers):
        threads.append(threading.current_thread())
        gate.wait(5)

    alerter = WebhookAlerter(URL, sender=slow)
    assert alerter.notify("a", "closed", "open")  # returns although sender is blocked
    assert not alerter.flush(0.05)
    gate.set()
    assert alerter.flush(2)
    assert threads[0] is not threading.current_thread() and threads[0].daemon


def test_sender_failure_is_swallowed_and_logged_without_secrets(caplog):
    def boom(url, body, headers):
        raise RuntimeError(f"cannot reach {url}")

    alerter = WebhookAlerter(URL, sender=boom)
    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        assert alerter.notify("a", "closed", "open")
        assert alerter.flush(2)
    text = caplog.text
    assert "delivery" in text and "chat.example.com" in text
    assert "SECRET-TOKEN" not in text and "cannot reach" not in text


def test_payload_never_contains_exception_text_or_url():
    rec = Recorder()
    alerter = make(rec, format="teams")
    breaker = CircuitBreaker(failure_threshold=1, name="x", on_state_change=alerter)

    @breaker
    def call():
        raise ConnectionError("password=hunter2")

    with pytest.raises(ConnectionError):
        call()
    alerter.flush(2)
    raw = json.dumps(rec.bodies)
    assert "hunter2" not in raw and "SECRET-TOKEN" not in raw


def test_event_subscription_path_and_close():
    rec = Recorder()
    alerter = make(rec).install()
    assert alerter.install() is alerter
    try:
        CircuitBreaker(failure_threshold=1, name="evt").force_open()
        alerter.flush(2)
        assert [b["breaker"] for b in rec.bodies] == ["evt"]
    finally:
        alerter.close()
    CircuitBreaker(failure_threshold=1, name="after").force_open()
    alerter.flush(2)
    assert len(rec.calls) == 1
    alerter.close()


@pytest.mark.parametrize(
    "kw",
    [{"format": "xml"}, {"on": ("broken",)}, {"cooldown": -1}, {"timeout": 0}],
)
def test_validation(kw):
    with pytest.raises(ValueError):
        WebhookAlerter(URL, **kw)


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        WebhookAlerter("file:///etc/passwd")
