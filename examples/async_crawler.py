"""Bulk-fetch many URLs politely: a global cap, a bulkhead per host and mirror racing.

``map_async`` keeps at most N fetches in flight overall, one ``Bulkhead`` per host keeps
any single site from being hammered, failures are collected instead of aborting the
batch, and ``race`` asks several mirrors at once and takes the first good answer. The
network is simulated with ``asyncio.sleep``.

Run: python examples/async_crawler.py
"""

from __future__ import annotations

import asyncio
from collections import Counter
from urllib.parse import urlsplit

from python_wrap.aio import map_async, race, run_sync
from python_wrap.bulkhead import Bulkhead

# --- simulated network -------------------------------------------------------------

LATENCY = {"fast.example": 0.002, "slow.example": 0.01, "mirror-a.example": 0.3}
in_flight: Counter[str] = Counter()
peak: Counter[str] = Counter()


async def http_get(url: str) -> str:
    host = urlsplit(url).netloc
    in_flight[host] += 1
    peak[host] = max(peak[host], in_flight[host])
    try:
        await asyncio.sleep(LATENCY.get(host, 0.005))
        if "broken" in url:
            raise ConnectionError(f"cannot reach {url}")
        return f"<html>{url}</html>"
    finally:
        in_flight[host] -= 1


# --- the crawler -------------------------------------------------------------------

PER_HOST = 2
_host_bulkheads: dict[str, Bulkhead] = {}


def bulkhead_for(host: str) -> Bulkhead:
    if host not in _host_bulkheads:
        _host_bulkheads[host] = Bulkhead(PER_HOST, timeout=5, name=host)
    return _host_bulkheads[host]


async def fetch(url: str) -> str:
    async with bulkhead_for(urlsplit(url).netloc):
        return await http_get(url)


async def fetch_from_mirrors(path: str, mirrors: list[str]) -> str:
    """First mirror to answer wins; the others are cancelled."""
    return await race(*(fetch(f"https://{m}{path}") for m in mirrors), timeout=2)


async def crawl(
    urls: list[str], *, limit: int = 8
) -> tuple[dict[str, str], dict[str, BaseException]]:
    results = await map_async(fetch, urls, limit=limit, return_exceptions=True)
    pages: dict[str, str] = {}
    errors: dict[str, BaseException] = {}
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, BaseException):
            errors[url] = result
        else:
            pages[url] = result
    return pages, errors


# --- demo ---


async def amain() -> None:
    urls = [f"https://{host}/page/{i}" for i in range(10) for host in LATENCY]
    urls.append("https://fast.example/broken")

    pages, errors = await crawl(urls)
    print(f"fetched {len(pages)} pages, {len(errors)} failed")
    assert len(pages) == 30
    assert list(errors) == ["https://fast.example/broken"]
    assert all(count <= PER_HOST for count in peak.values()), peak
    print("peak concurrency per host:", dict(peak))

    body = await fetch_from_mirrors("/file", ["mirror-a.example", "fast.example"])
    assert body == "<html>https://fast.example/file</html>"
    print("mirror race won by the fast mirror")


def main() -> None:
    run_sync(amain())  # the entry point of a sync script


if __name__ == "__main__":
    main()
