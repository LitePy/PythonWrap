from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support.app import SupportApp, build_app
from support.config import Settings

from python_wrap.chaos import chaos
from python_wrap.testing import fake_clock

TICKET = "My order A-1001 arrived broken, I am pat@example.com"


@pytest.fixture(autouse=True)
def _no_leftover_faults() -> Iterator[None]:
    yield
    chaos.clear()


@pytest.fixture
def clock() -> Iterator[object]:
    """Backoff sleeps cost nothing."""
    with fake_clock() as c:
        yield c


@pytest.fixture
def make_app(tmp_path: Path, clock: object) -> Callable[..., SupportApp]:
    def factory(**overrides: object) -> SupportApp:
        settings = Settings(tape_path=str(tmp_path / "tape.jsonl"), **overrides)  # type: ignore[arg-type]
        return build_app(settings)

    return factory
