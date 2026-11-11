from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orders.app import App, build_app
from orders.config import Settings


@pytest.fixture
def make_app(tmp_path: Path) -> Iterator[Callable[..., App]]:
    """Build apps with an outbox file in ``tmp_path``; closes every app afterwards."""
    apps: list[App] = []

    def factory(*, settings: Settings | None = None, **parts: object) -> App:
        settings = settings or Settings(outbox_path=str(tmp_path / "outbox.db"))
        app = build_app(settings, **parts)  # type: ignore[arg-type]
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()
