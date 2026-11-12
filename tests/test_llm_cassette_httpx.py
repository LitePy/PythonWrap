"""Contract tests against real httpx (skipped when httpx is not installed)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

from python_wrap.chaos import FaultInjector  # noqa: E402
from python_wrap.llm_cassette import Cassette, CassetteMiss  # noqa: E402
from python_wrap.llm_http_chaos import LLMHTTPFaults  # noqa: E402
from python_wrap.llm_testing import APIStatusError  # noqa: E402
from python_wrap.testing import fake_clock  # noqa: E402

URL = "https://llm.example/v1/messages"
KEY = "sk-ant-0123456789abcdefghijkl"


def api(request: Any) -> Any:
    body = json.loads(request.content)
    if body.get("stream"):
        sse = b"".join(f'data: {{"n": {i}}}\n\n'.encode() for i in range(4))
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
    return httpx.Response(
        200,
        json={"text": f"echo {body['prompt']}", "usage": {"input_tokens": 3, "output_tokens": 4}},
    )


def streaming_api(request: Any) -> Any:
    def chunks() -> Any:
        for i in range(4):
            yield f'data: {{"n": {i}}}\n\n'.encode()

    return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})


@pytest.fixture
def path(tmp_path: Path) -> str:
    return str(tmp_path / "http.json")


def post(client: Any, **body: Any) -> Any:
    return client.post(URL, json=body, headers={"x-api-key": KEY, "authorization": f"Bearer {KEY}"})


def test_record_replay_with_mock_transport(path: str) -> None:
    network = httpx.MockTransport(api)
    with httpx.Client(transport=Cassette(path).transport(network)) as client:
        live = post(client, prompt="hi")
    assert live.json()["text"] == "echo hi"

    def forbidden(request: Any) -> Any:
        raise AssertionError("replay must not reach the network")

    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(forbidden))) as client:
        replayed = post(client, prompt="hi")
    assert replayed.status_code == 200 and replayed.json() == live.json()
    assert KEY not in Path(path).read_text()


def test_replay_with_changed_key_and_unrecorded_request(path: str) -> None:
    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(api))) as client:
        post(client, prompt="hi")
    replay = Cassette(path, mode="replay")
    with httpx.Client(transport=replay.transport()) as client:
        other = client.post(URL, json={"prompt": "hi"}, headers={"x-api-key": "another-key-value"})
        assert other.json()["text"] == "echo hi"
        with pytest.raises(CassetteMiss):
            post(client, prompt="never recorded")


def test_streaming_response_round_trip_keeps_chunks(path: str) -> None:
    transport = Cassette(path).transport(httpx.MockTransport(streaming_api))
    with (
        httpx.Client(transport=transport) as client,
        client.stream("POST", URL, json={"s": 1}) as r,
    ):
        live = list(r.iter_bytes())
    assert b"".join(live).count(b"data:") == 4
    with (
        httpx.Client(transport=Cassette(path).transport()) as client,
        client.stream("POST", URL, json={"s": 1}) as r,
    ):
        replayed = list(r.iter_bytes())
        assert r.headers["content-type"] == "text/event-stream"
    assert replayed == live


def test_streaming_lines_replay(path: str) -> None:
    transport = Cassette(path).transport(httpx.MockTransport(streaming_api))
    with (
        httpx.Client(transport=transport) as client,
        client.stream("POST", URL, json={"s": 1}) as r,
    ):
        live = list(r.iter_lines())
    with (
        httpx.Client(transport=Cassette(path).transport()) as client,
        client.stream("POST", URL, json={"s": 1}) as r,
    ):
        assert list(r.iter_lines()) == live


def test_async_client_record_and_replay(path: str) -> None:
    async def main() -> tuple[Any, Any]:
        async with httpx.AsyncClient(
            transport=Cassette(path).transport(httpx.MockTransport(api))
        ) as client:
            live = await client.post(URL, json={"prompt": "hi"})
        async with httpx.AsyncClient(transport=Cassette(path).transport()) as client:
            return live.json(), (await client.post(URL, json={"prompt": "hi"})).json()

    live, replayed = asyncio.run(main())
    assert live == replayed


async def async_streaming_api(request: Any) -> Any:
    async def chunks() -> Any:
        for i in range(4):
            yield f'data: {{"n": {i}}}\n\n'.encode()

    return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})


def test_async_streaming_record_and_replay(path: str) -> None:
    async def run(transport: Any) -> list[bytes]:
        async with (
            httpx.AsyncClient(transport=transport) as client,
            client.stream("POST", URL, json={"s": 1}) as r,
        ):
            return [c async for c in r.aiter_bytes()]

    live = asyncio.run(run(Cassette(path).transport(httpx.MockTransport(async_streaming_api))))
    assert asyncio.run(run(Cassette(path).transport())) == live


def test_errors_are_recorded_and_replayed_in_order(path: str) -> None:
    calls = {"n": 0}

    def flaky(request: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": "slow"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(flaky))) as client:
        assert [post(client, p=1).status_code for _ in range(2)] == [429, 200]
    with httpx.Client(transport=Cassette(path).transport()) as client:
        first = post(client, p=1)
        assert (first.status_code, first.headers["retry-after"]) == (429, "3")
        assert post(client, p=1).status_code == 200


def test_gzip_responses_are_requested_uncompressed(path: str) -> None:
    seen: dict[str, str] = {}

    def spy(request: Any) -> Any:
        seen["encoding"] = request.headers.get("accept-encoding", "")
        return httpx.Response(200, json={"a": 1})

    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(spy))) as client:
        post(client, p=1)
    assert seen["encoding"] == "identity"


def test_default_inner_transport_requires_no_argument_for_replay(path: str) -> None:
    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(api))) as client:
        post(client, prompt="x")
    transport = Cassette(path).transport()
    with httpx.Client(transport=transport) as client:  # closes without an inner transport
        assert post(client, prompt="x").status_code == 200


# ---------------------------------------------------------------------------- chaos


@pytest.fixture
def faults() -> LLMHTTPFaults:
    return LLMHTTPFaults(FaultInjector())


def test_rate_limit_and_overload_become_real_http_responses(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(httpx.MockTransport(api))
    with httpx.Client(transport=transport) as client:
        with faults.rate_limit(times=1, retry_after=4):
            r = post(client, prompt="a")
            assert r.status_code == 429 and r.headers["retry-after"] == "4"
            assert r.json()["error"]["type"] == "rate_limit_error"
            assert post(client, prompt="a").status_code == 200
        with faults.overloaded(times=1):
            assert post(client, prompt="a").status_code == 529
        with faults.server_error(status=503, times=1):
            assert post(client, prompt="a").status_code == 503
        assert post(client, prompt="a").status_code == 200


def test_sdk_retry_logic_sees_the_faults(faults: LLMHTTPFaults) -> None:
    from python_wrap.llm import llm_retry, llm_retry_after

    transport = faults.transport(httpx.MockTransport(api))

    @llm_retry(max_attempts=4, delay=0.0)
    def ask(client: Any) -> str:
        response = post(client, prompt="a")
        response.raise_for_status()
        return str(response.json()["text"])

    with (
        httpx.Client(transport=transport) as client,
        fake_clock(),
        faults.rate_limit(times=2, retry_after=1),
    ):
        assert ask(client) == "echo a"
    assert llm_retry_after is not None


def test_timeout_and_stall_with_fake_clock(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(httpx.MockTransport(api))
    with httpx.Client(transport=transport) as client, fake_clock() as clock:
        with faults.timeout(times=1), pytest.raises(httpx.ReadTimeout):
            post(client, prompt="a")
        with faults.stall(seconds=20, times=1):
            assert post(client, prompt="a").status_code == 200
        assert 20 in clock.sleeps


def test_truncated_body_breaks_json_parsing(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(httpx.MockTransport(api))
    with httpx.Client(transport=transport) as client, faults.truncate(keep=0.5, times=1):
        r = post(client, prompt="a")
        assert r.status_code == 200
        with pytest.raises(json.JSONDecodeError):
            r.json()
        assert post(client, prompt="a").json()["text"] == "echo a"


def test_stream_stall_after_chunks(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(httpx.MockTransport(streaming_api))
    with (
        httpx.Client(transport=transport) as client,
        fake_clock() as clock,
        faults.stall(seconds=9, after_chunks=2, times=1),
        client.stream("POST", URL, json={"s": 1}) as r,
    ):
        assert len(list(r.iter_bytes())) == 4
    assert clock.sleeps == [9.0]


def test_async_faults(faults: LLMHTTPFaults) -> None:
    transport = faults.transport(httpx.MockTransport(api))

    async def main() -> list[int]:
        out = []
        async with httpx.AsyncClient(transport=transport) as client:
            with faults.rate_limit(times=1):
                out.append((await client.post(URL, json={"prompt": "a"})).status_code)
            with faults.truncate(times=1):
                r = await client.post(URL, json={"prompt": "a"})
                with pytest.raises(json.JSONDecodeError):
                    r.json()
            with faults.timeout(times=1), pytest.raises(httpx.ReadTimeout):
                await client.post(URL, json={"prompt": "a"})
            out.append((await client.post(URL, json={"prompt": "a"})).status_code)
        return out

    assert asyncio.run(main()) == [429, 200]


def test_chaos_over_replay_transport(path: str, faults: LLMHTTPFaults) -> None:
    with httpx.Client(transport=Cassette(path).transport(httpx.MockTransport(api))) as client:
        post(client, prompt="a")
    wrapped = faults.transport(Cassette(path).transport())
    with httpx.Client(transport=wrapped) as client, faults.rate_limit(times=1):
        assert post(client, prompt="a").status_code == 429
        assert post(client, prompt="a").status_code == 200


def test_global_injector_property_and_status_error_class() -> None:
    from python_wrap.chaos import chaos

    assert chaos.llm_http is chaos.llm_http
    assert isinstance(chaos.llm_http, LLMHTTPFaults)
    assert issubclass(APIStatusError, Exception)
