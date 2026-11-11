"""Entry point: ``python examples/projects/etl_pipeline/run.py`` runs one nightly batch."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etl_app.config import Settings
from etl_app.observability import build_observability, health_summary
from etl_app.pipeline import Pipeline
from etl_app.sources import SourceAPI


def main() -> int:
    pipeline = Pipeline(
        Settings.from_env(),
        {"billing": SourceAPI("billing", rows=10), "crm": SourceAPI("crm", rows=6)},
    )
    exporter, registry = build_observability(pipeline)
    with exporter:
        reports = pipeline.run()
        for report in reports.values():
            print(f"{report.source}: loaded={report.loaded} failed={report.failed}")
        print("health:", health_summary(registry)["status"])
        print(
            "\n".join(
                line for line in exporter.render().splitlines() if line.startswith("etl_health_up")
            )
        )
    assert pipeline.warehouse.writes == 16
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
