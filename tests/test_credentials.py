from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from python_wrap.credentials import expiring


def test_credential_invalidation_cannot_join_or_cache_old_refresh():
    started, release = threading.Event(), threading.Event()
    calls = []

    @expiring(refresh_margin=0)
    def load():
        calls.append(1)
        version = len(calls)
        if version == 1:
            started.set()
            assert release.wait(2)
        return str(version), 60

    with ThreadPoolExecutor(1) as pool:
        old = pool.submit(load)
        assert started.wait(1)
        load.invalidate()
        assert load() == "2"
        release.set()
        assert old.result() == "1"
        assert load() == "2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_async_invalidation_is_a_generation_barrier():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    @expiring(refresh_margin=0)
    async def load():
        calls.append(1)
        version = len(calls)
        if version == 1:
            started.set()
            await release.wait()
        return str(version), 60

    old = asyncio.create_task(load())
    await started.wait()
    load.invalidate()
    assert await load() == "2"
    release.set()
    assert await old == "1"
    assert await load() == "2"
    assert len(calls) == 2


def test_credential_keys_normalize_positional_keyword_defaults_and_version_name():
    calls = []

    @expiring(refresh_margin=0)
    def load(version="v1"):
        calls.append(version)
        return version, 60

    assert load() == load("v1") == load(version="v1") == "v1"
    assert calls == ["v1"]


@pytest.mark.asyncio
async def test_cancelled_credential_waiter_does_not_cancel_shared_refresh():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    @expiring(refresh_margin=0)
    async def load():
        calls.append(1)
        started.set()
        await release.wait()
        return "token", 60

    one = asyncio.create_task(load())
    await started.wait()
    two = asyncio.create_task(load())
    await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    release.set()
    assert await two == "token"
    assert len(calls) == 1


def test_credential_invalid_lifetime_and_methods():
    @expiring(refresh_margin=0)
    def invalid():
        return "token", float("nan")

    with pytest.raises(ValueError):
        invalid()

    class Client:
        @expiring(refresh_margin=0)
        def token(self, label="ok"):
            return label, 60

    client = Client()
    assert client.token(label="ok") == client.token("ok") == "ok"
    assert Client.token.cache_info().hits == 1


def test_expiring_and_buffered_preserve_sync_async_and_method_types():
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip("mypy")
    source = Path(__file__).parent / "typing_cases" / "credential_cases.py"
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(source)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
