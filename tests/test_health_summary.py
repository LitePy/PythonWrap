"""Overall health status and the WSGI/ASGI health endpoints."""

from __future__ import annotations

import asyncio
import json

import pytest

from python_wrap import CircuitBreaker, HealthRegistry


@pytest.fixture
def registry() -> HealthRegistry:
    return HealthRegistry()


def test_all_healthy_is_ok(registry):
    registry.register_check("db", lambda: True)
    summary = registry.summary()
    assert summary["status"] == "ok" and summary["http_status"] == 200
    assert summary["healthy"] == 1 and summary["unhealthy"] == []


def test_optional_failure_degrades_but_stays_in_rotation(registry):
    breaker = CircuitBreaker(failure_threshold=1, name="search")
    breaker.force_open()
    registry.register("search", breaker)
    registry.register_check("db", lambda: True)
    summary = registry.summary(critical=["db"])
    assert summary["status"] == "degraded" and summary["http_status"] == 200
    assert summary["unhealthy"] == ["search"]
    assert summary["components"]["search"]["state"] == "open"


def test_critical_failure_is_down(registry):
    registry.register_check("db", lambda: {"healthy": False, "lag": 3})
    registry.register_check("cache", lambda: True)
    assert registry.summary(critical=["db"])["status"] == "down"
    # Without a critical list every component counts.
    assert registry.summary()["http_status"] == 503


def test_crashing_check_is_reported_unhealthy(registry):
    def boom() -> bool:
        raise RuntimeError("password=secret")

    registry.register_check("vendor", boom)
    summary = registry.summary()
    assert summary["components"]["vendor"] == {"healthy": False, "error": "RuntimeError"}
    assert "secret" not in json.dumps(summary)


def test_wsgi_endpoint(registry):
    registry.register_check("db", lambda: False)
    seen = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(registry.wsgi_app()({}, start_response))
    assert seen["status"].startswith("503")
    assert seen["headers"]["Content-Type"] == "application/json"
    assert json.loads(body)["status"] == "down"
    registry.register_check("db", lambda: True)
    body = b"".join(registry.wsgi_app(critical=[])({}, start_response))
    assert seen["status"] == "200 OK" and json.loads(body)["status"] == "ok"


def test_asgi_endpoint(registry):
    registry.register_check("db", lambda: True)
    sent: list[dict[str, object]] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    app = registry.asgi_app()
    asyncio.run(app({"type": "http", "path": "/"}, receive, send))
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["status"] == "ok"  # type: ignore[arg-type]
    sent.clear()
    asyncio.run(app({"type": "lifespan"}, receive, send))
    assert sent == []
