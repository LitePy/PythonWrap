"""Opt-in isolation of successful memoized values and concurrent deliveries."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import memoize, singleflight
from python_wrap.testing import disable_caching
from test_caching_extras import DictStore


@pytest.mark.parametrize("store", [None, DictStore()])
def test_copies_loader_value_first_delivery_hits_and_peeks(store: Any) -> None:
    source = {"items": [1]}

    @memoize(copy_result=True, store=store)
    def load() -> dict[str, list[int]]:
        return source

    first = load()
    source["items"].append(2)
    first["items"].append(3)
    hit = load()
    peek = load.cache_get()
    assert hit == peek == {"items": [1]}
    hit["items"].append(4)
    peek["items"].append(5)
    assert load() == {"items": [1]}
    assert load.cache_parameters()["copy_result"] is True


def test_default_identity_and_disabled_cache_are_unchanged() -> None:
    source: list[int] = []

    @memoize
    def default() -> list[int]:
        return source

    @memoize(copy_result=True)
    def isolated() -> list[int]:
        return source

    assert default() is default() is default.cache_get() is source
    with disable_caching():
        assert isolated() is source
    assert not isolated.cache_contains()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [None, DictStore()])
@pytest.mark.parametrize("cacheable", [False, True])
async def test_async_shared_miss_delivers_independent_copies(store: Any, cacheable: bool) -> None:
    source = {"items": [1]}
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @memoize(copy_result=True, store=store, ttl=lambda result: 60 if cacheable else 0)
    async def load() -> dict[str, list[int]]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return source

    tasks = [asyncio.create_task(load()) for _ in range(3)]
    await entered.wait()
    release.set()
    first, second, third = await asyncio.gather(*tasks)
    assert calls == 1
    source["items"].append(2)
    first["items"].append(3)
    second["items"].append(4)
    assert third == {"items": [1]}
    if cacheable:
        peek = load.cache_get()
        peek["items"].append(5)
        assert await load() == {"items": [1]}
    else:
        assert not load.cache_contains()


def test_per_instance_cache_isolates_values() -> None:
    class Loader:
        @memoize(copy_result=True, per_instance=True)
        def load(self) -> list[int]:
            return [1]

    loader = Loader()
    loader.load().append(2)
    loader.load.cache_get(loader).append(3)
    assert loader.load() == [1]


def test_sync_singleflight_waiters_are_isolated() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    @memoize(copy_result=True)
    @singleflight
    def load() -> list[int]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return [1]

    # Holding the leader lets all subsequent calls overlap; joining a completed
    # cache hit is also valid, since both paths must deliver independent copies.
    results: list[list[int]] = []
    threads = [threading.Thread(target=lambda: results.append(load())) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert entered.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert len(results) == 4
    assert calls == 1
    for index, result in enumerate(results):
        result.append(index + 2)
    assert len({id(result) for result in results}) == 4
    assert load() == [1]


def test_sync_stale_refresh_copies_snapshot(fake_clock: FakeClock) -> None:
    source = [1]
    refreshed = threading.Event()

    @memoize(copy_result=True, ttl=10, stale_ttl=20)
    def load() -> list[int]:
        refreshed.set()
        return source

    load().append(7)
    source[:] = [2]
    refreshed.clear()
    fake_clock.advance(11)
    stale = load()
    assert stale == [1]
    stale.append(8)
    assert refreshed.wait(2)
    for thread in threading.enumerate():
        if thread.name.startswith("python_wrap-refresh-"):
            thread.join(2)
    source.append(9)
    assert load() == [2]


@pytest.mark.asyncio
async def test_async_stale_refresh_and_expired_joiners_are_isolated(fake_clock: FakeClock) -> None:
    source = [1]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @memoize(copy_result=True, ttl=10, stale_ttl=20)
    async def load() -> list[int]:
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            await release.wait()
        return source

    (await load()).append(7)
    source[:] = [2]
    fake_clock.advance(11)
    stale = await load()
    stale.append(8)
    await entered.wait()
    fake_clock.advance(20)
    tasks = [asyncio.create_task(load()) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(*tasks)
    assert calls == 2
    source.append(9)
    first.append(10)
    assert second == [2]
    assert await load() == [2]


@pytest.mark.parametrize("async_function", [False, True])
def test_copy_failure_is_explicit_and_never_stores_original(async_function: bool) -> None:
    cause = RuntimeError("cannot snapshot")

    class Uncopyable:
        def __deepcopy__(self, memo: Any) -> Any:
            raise cause

    source = Uncopyable()

    async def aload() -> Uncopyable:
        return source

    def sload() -> Uncopyable:
        return source

    load = memoize(copy_result=True)(aload if async_function else sload)
    with pytest.raises(TypeError, match="could not deepcopy") as caught:
        asyncio.run(load()) if async_function else load()
    assert caught.value.__cause__ is cause
    assert not load.cache_contains()


def test_cached_exceptions_are_never_copied() -> None:
    class UncopyableError(ValueError):
        def __deepcopy__(self, memo: Any) -> Any:
            raise AssertionError("exception copied")

    error = UncopyableError("original")

    @memoize(copy_result=True, cache_exceptions=(UncopyableError,))
    def load() -> list[int]:
        raise error

    for call in (load, load, load.cache_get):
        with pytest.raises(UncopyableError) as caught:
            call()
        assert caught.value is error


def test_deepcopy_can_inspect_cache_without_lock_reentrancy() -> None:
    class Inspecting:
        def __deepcopy__(self, memo: Any) -> Inspecting:
            load.cache_info()
            return Inspecting()

    @memoize(copy_result=True)
    def load() -> Inspecting:
        return Inspecting()

    completed: list[tuple[Inspecting, Inspecting, Inspecting]] = []
    thread = threading.Thread(
        target=lambda: completed.append((load(), load(), load.cache_get())), daemon=True
    )
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert len(completed) == 1


def test_copy_result_requires_bool() -> None:
    with pytest.raises(TypeError, match="copy_result must be bool"):
        memoize(copy_result=1)  # type: ignore[arg-type]
