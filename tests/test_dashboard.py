from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.dashboard import Dashboard
from python_wrap.exporters import PrometheusExporter
from python_wrap.health import HealthRegistry


def test_snapshot_content_and_redaction() -> None:
    class Pool:
        active = 2
        waiting = 1

    pool = Pool()
    registry = HealthRegistry()
    registry.register("pool", pool)
    with Dashboard(registry=registry, recent=3) as dash:
        _core.emit(
            "retry.attempt_failed",
            "svc",
            attempt=1,
            exception=ValueError("secret-token"),
            next_delay=0.5,
        )
        _core.emit("retry.gave_up", "svc", attempts=2, exception=None)
        _core.emit("cache.hit", "svc")
        _core.emit("circuit.state_changed", "db", old="closed", new="open")
    snap = dash.snapshot()
    assert json.loads(json.dumps(snap)) == snap
    assert snap["functions"]["svc"]["retries"] == 1
    assert snap["functions"]["svc"]["gave_up"] == 1
    assert snap["functions"]["svc"]["cache_hits"] == 1
    assert snap["breakers"] == {"db": "open"}
    assert snap["health"]["pool"] == {"healthy": True, "active": 2, "waiting": 1}
    assert snap["rates"]["retries"] == 1 and snap["rates"]["gave_up"] == 1
    assert [e["event"] for e in snap["events"]] == [
        "retry.gave_up",
        "cache.hit",
        "circuit.state_changed",
    ]
    # the oldest event (with the exception) fell out of the ring buffer; test redaction directly
    with Dashboard(registry=None) as dash2:
        _core.emit(
            "retry.attempt_failed",
            "svc",
            attempt=1,
            exception=ValueError("secret-token"),
            next_delay=0.5,
        )
    text = json.dumps(dash2.snapshot())
    assert "secret-token" not in text
    assert dash2.snapshot()["events"][0]["fields"]["exception"] == "ValueError"


def test_rates_window_follows_clock(fake_clock: Any) -> None:
    with Dashboard(registry=None) as dash:
        _core.emit("timeout.expired", "f", seconds=1.0)
        assert dash.snapshot()["rates"]["timeouts"] == 1
        fake_clock.advance(61)
        assert dash.snapshot()["rates"]["timeouts"] == 0


def test_includes_exporter_metrics() -> None:
    with (
        PrometheusExporter(registry=None) as exporter,
        Dashboard(registry=None, exporter=exporter) as dash,
    ):
        _core.emit("cache.hit", "f")
    assert dash.snapshot()["metrics"]["python_wrap_cache_hits_total"][0]["value"] == 1


def test_close_unsubscribes() -> None:
    dash = Dashboard(registry=None).install()
    dash.close()
    dash.close()
    _core.emit("cache.hit", "f")
    assert dash.snapshot()["events"] == []
    assert not _core.listeners


def test_html_is_self_contained() -> None:
    html = Dashboard(registry=None).render_html()
    assert "snapshot.json" in html and "setInterval(tick, 2000)" in html
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html and "<link" not in html


def test_recent_validation() -> None:
    with pytest.raises(ValueError):
        Dashboard(recent=0)


def call_wsgi(dash: Dashboard, path: str, method: str = "GET") -> tuple[str, dict[str, str], bytes]:
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(dash.wsgi_app({"PATH_INFO": path, "REQUEST_METHOD": method}, start_response))
    return seen["status"], seen["headers"], body


def test_wsgi_routes() -> None:
    dash = Dashboard(registry=None)
    status, headers, body = call_wsgi(dash, "/")
    assert status == "200 OK" and headers["Content-Type"].startswith("text/html")
    assert b"snapshot.json" in body
    status, headers, body = call_wsgi(dash, "/snapshot.json")
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["events"] == []
    assert call_wsgi(dash, "/nope")[0].startswith("404")
    assert call_wsgi(dash, "/", "POST")[0].startswith("405")


def test_asgi_routes() -> None:
    dash = Dashboard(registry=None)

    def run(path: str) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(dash.asgi_app({"type": "http", "method": "GET", "path": path}, receive, send))
        return sent

    html = run("/")
    assert html[0]["status"] == 200 and b"snapshot.json" in html[1]["body"]
    snap = run("/snapshot.json")
    assert json.loads(snap[1]["body"])["rates"]["window_seconds"] == 60.0
    assert run("/x")[0]["status"] == 404


def test_serve_on_port_zero_then_shutdown() -> None:
    dash = Dashboard(registry=None)
    server, thread = dash.serve()
    host, port = server.server_address[0], server.server_address[1]
    assert host == "127.0.0.1"
    try:
        _core.emit("cache.hit", "served")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot.json", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["functions"]["served"]["cache_hits"] == 1
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert b"python-wrap dashboard" in resp.read()
    finally:
        server.shutdown()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not _core.listeners
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
