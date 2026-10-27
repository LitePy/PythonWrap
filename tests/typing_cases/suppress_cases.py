"""Type-checking cases (not collected by pytest); verified by tests/test_typing.py."""

from __future__ import annotations

from python_wrap import suppress


@suppress
def bare(d: dict[str, int]) -> int:
    return d["x"]


@suppress(KeyError)
def one(d: dict[str, int]) -> int:
    return d["x"]


@suppress(KeyError, ValueError, logger="x")
async def many(x: int) -> int:
    return x


@suppress()
def empty(x: int) -> int:
    return x


reveal_type(bare)  # noqa: F821  # R: int | None
reveal_type(one)  # noqa: F821  # R: int | None
reveal_type(many)  # noqa: F821  # R: Coroutine[Any, Any, int | None]
reveal_type(empty)  # noqa: F821  # R: int | None
