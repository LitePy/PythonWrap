"""Policy references, watched sources, state carried across reloads, config events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from python_wrap import events
from python_wrap.circuit import CircuitBreaker
from python_wrap.errors import CircuitOpenError, RateLimitExceeded
from python_wrap.policies import EnvSource, FileSource, PolicyRegistry, RedisSource


def write(path: Path, mapping: dict[str, Any]) -> None:
    path.write_text(json.dumps(mapping), encoding="utf-8")


def admitted(call: Any, tries: int = 10) -> int:
    count = 0
    for _ in range(tries):
        try:
            call()
        except RateLimitExceeded:
            continue
        count += 1
    return count


def test_names_resolve_through_registries_only() -> None:
    registry = PolicyRegistry()
    backend = object()
    registry.register_backend("default", backend)
    registry.register(
        "db",
        retry={"max_attempts": 2, "delay": 0, "on": ["builtins.ConnectionError"]},
        breaker={"failure_threshold": 1, "on": "builtins.TimeoutError"},
    )
    breaker = registry._compiled["db"]._roles["breaker"][0]
    assert breaker.on == (TimeoutError,)
    assert registry._resolve("retry_if", "db_transient") is not None
    assert registry._resolve(None, "@default") is backend
    for bad in (
        {"retry": {"on": ["os.system"]}},
        {"retry": {"retry_if": "eval"}},
        {"limiter": {"backend": "@missing"}},
    ):
        with pytest.raises(ValueError, match="register"):
            registry.load_from_mapping({"x": bad})

    class VendorTimeout(Exception):
        pass

    registry.register_exception("vendor.Timeout", VendorTimeout)
    registry.register("vendor", retry={"max_attempts": 2, "on": ["vendor.Timeout"]})


def test_watch_applies_changes_keeps_open_breaker_and_reports(tmp_path: Path) -> None:
    path = tmp_path / "policies.json"
    write(
        path, {"api": {"limiter": {"calls": 2, "period": 60}, "breaker": {"failure_threshold": 1}}}
    )
    registry = PolicyRegistry()
    seen: list[tuple[str, dict[str, Any]]] = []
    unsubscribe = events.subscribe(lambda e: seen.append((e.name, dict(e.fields))))
    watch = registry.watch(FileSource(path), interval=3600)
    try:

        @registry.resilient("api")
        def call() -> str:
            return "ok"

        @registry.resilient("api")
        def boom() -> None:
            raise ConnectionError

        assert admitted(call) == 2
        with pytest.raises(ConnectionError):
            boom()
        with pytest.raises(CircuitOpenError):
            call()

        write(
            path,
            {"api": {"limiter": {"calls": 5, "period": 60}, "breaker": {"failure_threshold": 3}}},
        )
        assert watch.poll()
        breaker = registry._compiled["api"]._roles["breaker"][0]
        assert isinstance(breaker, CircuitBreaker) and breaker.failure_threshold == 3
        with pytest.raises(CircuitOpenError):  # still open under the new thresholds
            call()
        breaker.reset()
        assert admitted(call) == 5

        path.write_text("{not json", encoding="utf-8")
        assert not watch.poll()
        assert registry.describe("api")["api"]["generation"] == 2
    finally:
        watch.stop()
        unsubscribe()
    names = [name for name, _ in seen]
    assert names.count("config.changed") == 2
    assert ("config.rejected", {"error": "JSONDecodeError"}) in seen


def test_unchanged_mapping_keeps_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PW_TESTPOL_SVC", json.dumps({"timeout": 2}))
    registry = PolicyRegistry()
    watch = registry.watch(EnvSource("PW_TESTPOL_"), interval=3600)
    try:
        assert not watch.poll()
        registry.load_from_mapping({"SVC": {"timeout": 2}})
        assert registry.describe("SVC")["SVC"]["generation"] == 1
    finally:
        watch.stop()


def test_redis_source_reads_one_key() -> None:
    class Client:
        def get(self, key: str) -> bytes | None:
            return b'{"svc": {"timeout": 1}}' if key == "python-wrap:policies" else None

    assert RedisSource(Client()).read() == {"svc": {"timeout": 1}}
    assert RedisSource(Client(), key="other").read() == {}


def test_global_registry_reports_generations_in_health() -> None:
    from python_wrap.health import health
    from python_wrap.policies import policies

    policies.register("health-probe", timeout=1)
    report = health.report()
    assert report["policies"]["generations"]["health-probe"] >= 1
