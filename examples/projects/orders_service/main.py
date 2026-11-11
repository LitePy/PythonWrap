"""Run the orders service end to end against fakes: ``python main.py``.

Prints what happened at each step and finishes in well under two seconds.
"""

from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orders.app import build_app
from orders.config import Settings
from orders.fakes import FakeMailer, FakePaymentGateway
from orders.outbox import close_outbox, make_outbox
from orders.telemetry import Metrics

from python_wrap import RateLimitExceeded
from python_wrap.doctor import explain
from python_wrap.testing import fake_clock


def main() -> None:
    metrics = Metrics()
    with tempfile.TemporaryDirectory() as tmp, fake_clock() as clock, metrics.listen():
        settings = Settings(outbox_path=os.path.join(tmp, "outbox.db"))
        gateway = FakePaymentGateway(failures=2)  # the first two attempts will fail
        app = build_app(settings, gateway=gateway)

        # 1. Inventory: 20 concurrent lookups of one product cost a single query.
        with ThreadPoolExecutor(20) as pool:
            list(pool.map(app.inventory.lookup, ["mug"] * 20))
        print(f"1. 20 concurrent stock lookups -> {len(app.db.queries)} database query")

        # 2. Payment: two transport errors are retried with backoff (no real sleeping).
        order = app.create_order("ana", "req-1", [("mug", 2)], "tok_visa")
        print(
            f"2. order {order['order_id']} paid after {gateway.attempts} gateway attempts, "
            f"{clock.total_slept:.1f}s of fake backoff"
        )

        # 3. Idempotent creation: the client retries the same request.
        again = app.create_order("ana", "req-1", [("mug", 2)], "tok_visa")
        assert again == order and len(gateway.charges) == 1
        print(f"3. duplicate request returned the same order; charges made: {len(gateway.charges)}")

        # 4. Per-user rate limit.
        for n in range(2, 4):
            app.create_order("ana", f"req-{n}", [("mug", 1)], "tok_visa")
        try:
            app.create_order("ana", "req-4", [("mug", 1)], "tok_visa")
        except RateLimitExceeded as exc:
            print(f"4. ana's 4th order refused, retry in {exc.retry_after:.0f}s")

        # 5. Health summary.
        app.outbox.flush()
        summary = app.health.summary(critical=["payments"])
        print(f"5. health: {summary['status']} ({summary['healthy']} healthy components)")

        # 6. doctor: lint the payments policy.
        report = explain(app.payments)
        print(f"6. doctor on payments policy: ok={report.ok}, findings={len(report.findings)}")
        app.close()
        delivered = len(app.mailer.sent)

        # 7. The mail relay is down: orders still succeed, mail waits in the journal.
        down = FakeMailer(down=True)
        outbox = make_outbox(settings.outbox_path, down, 0.01)
        outbox.submit({"id": "o-100", "to": "ben", "subject": "Order o-100 confirmed"})
        outbox.flush()
        pending = len(outbox.journal.pending()) if outbox.journal else 0
        close_outbox(outbox)
        # ...and after a restart the journal is replayed.
        recovered = FakeMailer()
        restarted = make_outbox(settings.outbox_path, recovered, 0.01)
        replayed = restarted.replay()
        restarted.flush()
        close_outbox(restarted)
        print(f"7. relay down: {pending} mail journaled; after restart replayed {replayed}")
        assert set(recovered.sent) == {"o-100"}

    print(f"8. {delivered} confirmation emails delivered; metrics: {metrics.render()}")


if __name__ == "__main__":
    main()
