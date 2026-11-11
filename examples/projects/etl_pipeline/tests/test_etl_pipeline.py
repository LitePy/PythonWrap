"""Happy path plus the failure scenarios the pipeline is built to survive."""

from __future__ import annotations

import dataclasses
import time

import pytest
from etl_app import reference
from etl_app.config import Settings
from etl_app.observability import build_observability, health_summary
from etl_app.pipeline import Pipeline
from etl_app.sources import SourceAPI

from python_wrap import BulkheadFull
from python_wrap.chaos import chaos


@pytest.fixture(autouse=True)
def clean() -> None:
    reference.exchange_rates.cache_clear()
    reference.lookups["count"] = 0
    chaos.clear()


def make(**overrides: float) -> Pipeline:
    settings = dataclasses.replace(Settings(), **overrides)  # type: ignore[arg-type]
    apis = {"billing": SourceAPI("billing", 10), "crm": SourceAPI("crm", 6)}
    return Pipeline(settings, apis)


def test_happy_path_loads_everything_once() -> None:
    pipeline = make()
    exporter, registry = build_observability(pipeline)
    with exporter:
        reports = pipeline.run()
        text = exporter.render()
    assert {r.source: r.loaded for r in reports.values()} == {"billing": 10, "crm": 6}
    assert pipeline.warehouse.writes == 16
    assert reference.lookups["count"] == 1  # reference data fetched once for both sources
    # Several chunks per source, but the rate-limited status endpoint was pinged once each.
    assert [len(a.progress_pings) for a in pipeline.apis.values()] == [1, 1]
    assert 'etl_health_up{component="source-billing"} 1' in text
    assert health_summary(registry)["status"] == "ok"


def test_deadline_stops_cooperatively_and_resume_loads_the_rest() -> None:
    apis = {"slow": SourceAPI("slow", 12, latency=0.03)}
    pipeline = Pipeline(dataclasses.replace(Settings(), chunk_size=2, source_deadline=0.12), apis)
    first = pipeline.run()["slow"]
    assert first.timed_out
    time.sleep(0.15)  # let the worker reach its next checkpoint() and stop
    saved = pipeline.checkpoints["slow"].load()
    assert 0 < saved < 12  # progress was kept
    pipeline.settings = dataclasses.replace(pipeline.settings, source_deadline=30.0)
    second = pipeline.run()["slow"]
    assert not second.timed_out and second.loaded == 12 - saved
    assert len(pipeline.warehouse.rows) == 12
    assert pipeline.warehouse.writes == 12  # a replayed chunk wrote nothing twice


def test_chaos_flaky_fetch_is_retried_then_gives_up() -> None:
    pipeline = make()
    with chaos.inject("etl.fetch", exception=ConnectionError, times=2):
        reports = pipeline.run()
    assert all(r.error is None for r in reports.values())  # 2 failures, 3 attempts

    broken = make()
    _, registry = build_observability(broken)
    with chaos.inject("etl.fetch", exception=ConnectionError, times=100):
        reports = broken.run()
    assert {r.error for r in reports.values()} == {"ConnectionError"}
    assert broken.warehouse.writes == 0
    summary = health_summary(registry)
    assert summary["status"] == "degraded" and "source-crm" in summary["unhealthy"]


def test_bulkhead_isolates_a_saturated_source() -> None:
    pipeline = make()
    crm = pipeline.bulkheads["crm"]
    with crm, crm:  # both crm slots are busy elsewhere
        reports = pipeline.run()
    assert reports["crm"].error == BulkheadFull.__name__
    assert reports["billing"].loaded == 10
