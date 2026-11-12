from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from orders.app import App
from orders.config import Settings
from orders.fakes import FakeMailer, FakePaymentGateway
from orders.inventory import OutOfStock
from orders.outbox import close_outbox, make_outbox
from orders.payments import PaymentDeclined
from orders.telemetry import Metrics

from python_wrap import RateLimitExceeded
from python_wrap.doctor import explain
from python_wrap.testing import fake_clock

MakeApp = Callable[..., App]


def test_happy_path_charges_reserves_and_queues_mail(make_app: MakeApp) -> None:
    app = make_app()
    order = app.create_order("ana", "r1", [("mug", 2)], "tok_visa")

    assert order["total_cents"] == 2400
    assert len(app.gateway.charges) == 1
    assert app.db.products["mug"][0] == 48  # stock reserved
    app.outbox.flush()
    assert list(app.mailer.sent) == ["ana-r1"]
    assert app.create_order.stats.calls == 1


def test_duplicate_request_returns_the_stored_order(make_app: MakeApp) -> None:
    app = make_app()
    first = app.create_order("ana", "r1", [("mug", 1)], "tok_visa")
    second = app.create_order("ana", "r1", [("mug", 1)], "tok_visa")

    assert second == first
    assert app.gateway.attempts == 1
    assert app.db.products["mug"][0] == 49  # reserved once


def test_concurrent_stock_lookups_share_one_query(make_app: MakeApp) -> None:
    app = make_app()
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(app.inventory.lookup, ["poster"] * 16))
    assert {r["stock"] for r in results} == {5}
    assert app.db.queries == ["poster"]


def test_rate_limit_blocks_one_user_not_another(make_app: MakeApp, tmp_path: Path) -> None:
    app = make_app(settings=Settings(orders_per_minute=2, outbox_path=str(tmp_path / "o.db")))
    app.create_order("ana", "r1", [("mug", 1)], "tok_visa")
    app.create_order("ana", "r2", [("mug", 1)], "tok_visa")
    with pytest.raises(RateLimitExceeded):
        app.create_order("ana", "r3", [("mug", 1)], "tok_visa")
    assert app.create_order("ben", "r1", [("mug", 1)], "tok_visa")["order_id"] == "ben-r1"
    # A replay of an accepted request is answered from the store, not rate limited.
    assert app.create_order("ana", "r1", [("mug", 1)], "tok_visa")["order_id"] == "ana-r1"


def test_transient_gateway_errors_are_retried(make_app: MakeApp) -> None:
    metrics = Metrics()
    with fake_clock() as clock, metrics.listen():
        app = make_app(gateway=FakePaymentGateway(failures=2))
        order = app.create_order("ana", "r1", [("mug", 1)], "tok_visa")
    assert order["charge_id"] == "ch_1"
    assert app.gateway.attempts == 3
    assert clock.total_slept > 0  # backoff happened, instantly
    assert metrics.counts["retry.attempt_failed"] == 2


def test_gateway_outage_fails_cleanly(make_app: MakeApp) -> None:
    """The failure scenario: nothing is reserved, nothing is emailed, nothing is stored."""
    with fake_clock():
        app = make_app(gateway=FakePaymentGateway(down=True))
        with pytest.raises(ConnectionError):
            app.create_order("ana", "r1", [("mug", 1)], "tok_visa")
        assert app.gateway.attempts == app.settings.payment_attempts
        assert app.db.products["mug"][0] == 50
        app.outbox.flush()
        assert app.mailer.sent == {}

        # The failure was not remembered as an answer: once the gateway is back,
        # the same request id succeeds.
        app.gateway.down = False
        assert app.create_order("ana", "r1", [("mug", 1)], "tok_visa")["order_id"] == "ana-r1"


def test_declined_card_is_not_retried(make_app: MakeApp) -> None:
    app = make_app()
    with pytest.raises(PaymentDeclined):
        app.create_order("ana", "r1", [("mug", 1)], "tok_declined")
    assert app.gateway.attempts == 1


def test_out_of_stock_never_reaches_the_gateway(make_app: MakeApp) -> None:
    app = make_app()
    with pytest.raises(OutOfStock):
        app.create_order("ana", "r1", [("poster", 6)], "tok_visa")
    assert app.gateway.attempts == 0


def test_outbox_replays_after_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "outbox.db")
    down = make_outbox(path, FakeMailer(down=True), 0.01)
    down.submit({"id": "m1", "to": "ana", "subject": "hello"})
    down.flush()
    assert down.journal is not None and len(down.journal.pending()) == 1
    close_outbox(down)

    mailer = FakeMailer()
    restarted = make_outbox(path, mailer, 0.01)
    assert restarted.replay() == 1
    restarted.flush()
    assert list(mailer.sent) == ["m1"]
    assert restarted.journal is not None and restarted.journal.pending() == []
    close_outbox(restarted)


def test_health_summary_reflects_the_gateway(make_app: MakeApp) -> None:
    app = make_app()
    assert app.health.summary(critical=["payments"])["status"] == "ok"
    app.gateway.down = True
    summary = app.health.summary(critical=["payments"])
    assert (summary["status"], summary["http_status"]) == ("down", 503)
    assert app.health.summary(critical=["email_outbox"])["status"] == "degraded"


def test_doctor_finds_no_problems_in_the_policies(make_app: MakeApp) -> None:
    app = make_app()
    report = explain(app.payments, idempotent=True)
    assert report.ok, str(report)
    assert report.findings == ()
    roles = [layer.role for layer in report.layers]
    assert roles == ["breaker", "retry", "timeout"]


def test_doctor_rejects_retries_if_the_charge_were_not_idempotent(make_app: MakeApp) -> None:
    """What `doctor` is for: it flags retried money movement without an idempotency key."""
    app = make_app()
    report = explain(app.payments, idempotent=False)
    assert not report.ok
    assert report.findings[0].code == "PW002"
