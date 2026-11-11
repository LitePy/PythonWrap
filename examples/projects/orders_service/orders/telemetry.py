"""Metrics: count every python-wrap event by name (retries, cache hits, rejections...)."""

from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager

from python_wrap.events import Event, listening


class Metrics:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, event: Event) -> None:
        self.counts[event.name] += 1

    def listen(self) -> AbstractContextManager[object]:
        """Use as ``with metrics.listen(): ...``."""
        return listening(self)

    def render(self) -> str:
        return ", ".join(f"{name}={n}" for name, n in sorted(self.counts.items()))
