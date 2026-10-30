"""Static regression checks for public 0.3 signatures and changed return types."""

from typing_extensions import assert_type

from python_wrap import (
    BatchResult,
    aprocess_batches,
    expiring,
    idempotent,
    memoize,
    process_batches,
    retry,
    retrying,
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


@retry(delays=[5, 30, 120], log_values=False)
def scheduled(value: int) -> str:
    return str(value)


assert_type(scheduled(1), str)
scheduled("bad")  # type: ignore[arg-type]


async def scheduled_async() -> None:
    @retry(delays=(1, 2), log_values=False)
    async def operation(value: int) -> str:
        return str(value)

    assert_type(await operation(1), str)
    async for attempt in retrying(delays=[0, 1], log_values=False):
        async with attempt:
            await operation(1)


@memoize(copy_result=True)
def isolated(value: int) -> list[int]:
    return [value]


assert_type(isolated(1), list[int])
isolated("bad")  # type: ignore[arg-type]
