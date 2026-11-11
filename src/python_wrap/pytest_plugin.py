"""Opt-in pytest fixtures for LLM cassettes and HTTP-level chaos.

Enable it explicitly, in a top-level ``conftest.py``::

    pytest_plugins = ["python_wrap.pytest_plugin"]

It is deliberately not registered through the ``pytest11`` entry point: installing the
library must never change how an unrelated test suite starts. Fixtures:

- ``llm_cassette``: a factory ``llm_cassette(name=None, **options) -> Cassette`` whose file
  lives in ``<test dir>/cassettes/<test module>/<test name>.json`` (override the folder with
  ``--llm-cassette-dir``); saved when the test ends. Build the client with
  ``transport=llm_cassette().transport()``. The mode comes from ``--llm-cassette-mode``
  (``once`` by default; use ``replay`` in CI so an unrecorded request fails instead of
  calling the network) or the ``PYTHON_WRAP_CASSETTE_MODE`` environment variable.
- ``chaos_llm_http``: a fresh `python_wrap.llm_http_chaos.LLMHTTPFaults` on its own
  injector; add rules and wrap your transport with ``chaos_llm_http.transport(inner)``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from python_wrap.chaos import FaultInjector
from python_wrap.llm_cassette import Cassette
from python_wrap.llm_http_chaos import LLMHTTPFaults

__all__ = ["chaos_llm_http", "llm_cassette", "pytest_addoption"]

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--llm-cassette-mode`` and ``--llm-cassette-dir``."""
    group = parser.getgroup("python-wrap")
    group.addoption(
        "--llm-cassette-mode",
        default=None,
        choices=("once", "record", "replay", "new"),
        help="how llm_cassette behaves (default: PYTHON_WRAP_CASSETTE_MODE or 'once')",
    )
    group.addoption(
        "--llm-cassette-dir",
        default=None,
        help="folder for cassette files (default: <test dir>/cassettes)",
    )


def _slug(text: str) -> str:
    return _SAFE.sub("_", text).strip("._") or "cassette"


@pytest.fixture
def llm_cassette(request: pytest.FixtureRequest) -> Iterator[Callable[..., Cassette]]:
    """Factory for `Cassette` objects named after the running test."""
    made: list[Cassette] = []
    option = request.config.getoption("--llm-cassette-mode", default=None)
    mode = option or os.environ.get("PYTHON_WRAP_CASSETTE_MODE") or "once"
    root = request.config.getoption("--llm-cassette-dir", default=None)
    base = Path(root) if root else Path(str(request.node.path)).parent / "cassettes"

    def make(name: str | None = None, **options: Any) -> Cassette:
        label = _slug(name) if name else _slug(request.node.name)
        folder = base / _slug(request.node.path.stem)
        options.setdefault("mode", mode)
        cassette = Cassette(folder / f"{label}.json", **options)
        made.append(cassette)
        return cassette

    yield make
    for cassette in made:
        if cassette.recorded:
            cassette.save()


@pytest.fixture
def chaos_llm_http() -> LLMHTTPFaults:
    """HTTP-level LLM fault rules on a private injector, cleaned up with the test."""
    return LLMHTTPFaults(FaultInjector())
