"""Settings, read once at start-up (environment variables override the defaults)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    chunk_size: int = 4  # rows between checkpoints
    source_deadline: float = 30.0  # seconds one source may take per run
    max_parallel_per_source: int = 2  # bulkhead width on each source API
    progress_interval: float = 60.0  # at most one progress ping a minute per source

    @classmethod
    def from_env(cls) -> Settings:
        base = cls()
        return cls(
            chunk_size=int(os.environ.get("ETL_CHUNK_SIZE", base.chunk_size)),
            source_deadline=float(os.environ.get("ETL_SOURCE_DEADLINE", base.source_deadline)),
        )
