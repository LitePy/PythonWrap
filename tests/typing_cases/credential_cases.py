from __future__ import annotations

from typing import Any

from python_wrap.buffering import buffered
from python_wrap.credentials import expiring


@expiring(refresh_margin=1)
def token(account: int) -> tuple[str, float]:
    return str(account), 60


value: str = token(1)
token.invalidate()
info: int = token.cache_info().hits
token("not-an-int")  # type: ignore[arg-type]
wrong_value: int = token(1)  # type: ignore[assignment]


@expiring(refresh_margin=1)
async def async_token(account: int) -> tuple[str, float]:
    return str(account), 60


class Service:
    @expiring(refresh_margin=1)
    def token(self, account: int) -> tuple[str, float]:
        return str(account), 60


method_value: str = Service().token(1)
Service().token("not-an-int")  # type: ignore[arg-type]


@buffered()
def write(rows: list[Any]) -> None:
    pass


@buffered()
async def async_write(rows: list[Any]) -> None:
    pass


accepted: bool = write.submit(1)
closed: bool = write.close()


async def checks() -> None:
    result: str = await async_token(1)
    wrong: int = await async_token(1)  # type: ignore[assignment]
    accepted: bool = await async_write.submit(1)
    closed: bool = await async_write.close()
    assert result and not wrong and accepted and closed
