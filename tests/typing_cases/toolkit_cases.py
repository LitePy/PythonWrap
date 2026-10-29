"""Static regression checks for public 0.3 signatures and changed return types."""

from typing_extensions import assert_type

from python_wrap import (
    BatchResult,
    aprocess_batches,
    expiring,
    idempotent,
    process_batches,
    single_instance,
    switch,
    timed,
)


@switch("optional", when_off="skip")
def optional(value: int) -> str:
    return str(value)


assert_type(optional(1), str | None)
optional("bad")  # type: ignore[arg-type]


@idempotent()
@single_instance()
@timed()
def preserved(value: int) -> str:
    return str(value)


assert_type(preserved(1), str)
preserved("bad")  # type: ignore[arg-type]


@expiring(refresh_margin=1)
def credential(key: int) -> tuple[str, float]:
    return str(key), 60.0


assert_type(credential(1), str)
credential("bad")  # type: ignore[arg-type]
credential.invalidate()
assert_type(process_batches([1, 2], str), BatchResult[int, str])


async def check_async() -> None:
    @switch("optional", when_off="skip")
    async def optional_async(value: int) -> str:
        return str(value)

    assert_type(await optional_async(1), str | None)

    @expiring(refresh_margin=1)
    async def async_credential(key: int) -> tuple[str, float]:
        return str(key), 60.0

    assert_type(await async_credential(1), str)
    async_credential.invalidate()

    async def handler(value: int) -> str:
        return str(value)

    assert_type(await aprocess_batches([1, 2], handler), BatchResult[int, str])
