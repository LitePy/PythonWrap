"""Run the sources in parallel, each behind its own bulkhead, deadline and checkpoint."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from python_wrap import (
    Bulkhead,
    MemoryCheckpoint,
    checkpoint,
    process_batches,
    retry,
    throttle,
    timeout,
)

from .config import Settings
from .reference import exchange_rates
from .sources import SourceAPI
from .warehouse import Warehouse, make_loader


@dataclass
class SourceReport:
    source: str
    loaded: int = 0
    failed: int = 0
    timed_out: bool = False
    error: str | None = None


@dataclass
class Pipeline:
    settings: Settings
    apis: dict[str, SourceAPI]
    warehouse: Warehouse = field(default_factory=Warehouse)
    checkpoints: dict[str, MemoryCheckpoint] = field(default_factory=dict)
    bulkheads: dict[str, Bulkhead] = field(init=False)
    last: dict[str, SourceReport] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # One bulkhead per source: a slow system cannot use up the other's slots.
        self.bulkheads = {
            name: Bulkhead(self.settings.max_parallel_per_source, max_waiting=0, name=f"src-{name}")
            for name in self.apis
        }
        self.load_row = make_loader(self.warehouse)

        # The status endpoint is rate limited: one ping per interval per source. Extra calls
        # are skipped (throttle returns the last result instead of calling the API).
        def ping(api: SourceAPI, done: int) -> None:
            api.ping_progress(done)

        self.ping = throttle(self.settings.progress_interval, key=lambda api, done: api.name)(ping)

    def _rows(self, api: SourceAPI) -> list[dict[str, Any]]:
        fetch = retry(max_attempts=3, delay=0.0, on=ConnectionError)(api.fetch_rows)
        with self.bulkheads[api.name]:
            return fetch()

    def run_source(self, name: str) -> SourceReport:
        api = self.apis[name]
        report = SourceReport(name)
        cp = self.checkpoints.setdefault(name, MemoryCheckpoint())
        rates = exchange_rates()  # memoized reference data

        def guarded(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
            for row in rows:
                checkpoint()  # cooperative stop: raises once the deadline has passed
                yield row

        def handle(row: dict[str, Any]) -> str:
            api.row_work()
            return self.load_row(
                {**row, "amount_eur": round(row["amount"] / rates[row["currency"]], 2)}
            )

        # use_signals=False: the work runs in a worker thread and stops itself at checkpoint().
        @timeout(self.settings.source_deadline, use_signals=False)
        def work() -> int:
            result = process_batches(
                guarded(self._rows(api)),
                handle,
                chunk_size=self.settings.chunk_size,
                checkpoint=cp,
                on_progress=lambda done, next_index: self.ping(api, next_index),
            )
            report.failed += len(result.failed)
            return len(result.succeeded)

        try:
            report.loaded = work()
        except TimeoutError:
            report.timed_out = True  # the checkpoint keeps what finished; resume next run
        except Exception as exc:
            report.error = type(exc).__name__
        return report

    def source_ok(self, name: str) -> bool:
        last = self.last.get(name)
        return last is None or (last.error is None and not last.timed_out)

    def run(self) -> dict[str, SourceReport]:
        names = list(self.apis)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
            self.last = dict(zip(names, pool.map(self.run_source, names), strict=True))
        return self.last
