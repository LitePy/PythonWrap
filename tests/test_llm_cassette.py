"""Cassettes with duck-typed requests (no httpx needed): matching, redaction, failures."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from python_wrap.llm_cassette import (
    Cassette,
    CassetteError,
    CassetteMiss,
    RecordingTransport,
    ReplayTransport,
    build_response,
)

URL = "https://llm.example/v1/chat?api-version=1"
SECRET = "sk-live-0123456789abcdefABCDEF"


class Req:
    def __init__(
        self,
        body: Any = None,
        *,
        method: str = "POST",
        url: str = URL,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> None:
        self.method, self.url = method, url
        self.headers = headers if headers is not None else {"authorization": f"Bearer {SECRET}"}
        self.content = raw if raw is not None else json.dumps(body or {}).encode()


class Wire:
    """A fake network: counts calls and returns scripted (status, headers, chunks)."""

    def __init__(self, *replies: tuple[int, list[tuple[str, str]], list[bytes]]) -> None:
        self.replies = list(replies) or [
            (200, [("content-type", "application/json")], [b'{"ok": 1}'])
        ]
        self.calls: list[Req] = []

    def handle_request(self, request: Any) -> Any:
        self.calls.append(request)
        status, headers, chunks = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return build_response(request, status, headers, chunks, asynchronous=False)

    async def handle_async_request(self, request: Any) -> Any:
        self.calls.append(request)
        status, headers, chunks = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return build_response(request, status, headers, chunks, asynchronous=True)


def read_all(response: Any) -> bytes:
    return b"".join(response.stream) if hasattr(response.stream, "__iter__") else response.read()


@pytest.fixture
def path(tmp_path: Path) -> str:
    return str(tmp_path / "c" / "chat.json")


def record(path: str, wire: Wire, *requests: Req, **options: Any) -> Cassette:
    cassette = Cassette(path, **options)
    transport = cassette.transport(wire)
    for request in requests:
        read_all(transport.handle_request(request))
    return cassette


class TestRoundTrip:
    def test_record_then_replay_without_network(self, path: str) -> None:
        wire = Wire()
        cassette = record(path, wire, Req({"model": "m", "n": 1}))
        assert cassette.recorded == 1 and os.path.exists(path)
        replay = Cassette(path)
        assert replay.replaying and not replay.recording
        response = replay.transport().handle_request(Req({"model": "m", "n": 1}))
        assert response.status_code == 200
        assert read_all(response) == b'{"ok": 1}'
        assert len(wire.calls) == 1
        assert replay.replayed == 1 and replay.unused == 0

    def test_once_mode_records_when_missing_then_replays(self, path: str) -> None:
        wire = Wire()
        assert Cassette(path).recording
        record(path, wire, Req({"a": 1}))
        again = Cassette(path)
        assert isinstance(again.transport(), ReplayTransport)
        assert isinstance(Cassette(path, mode="record").transport(wire), RecordingTransport)

    def test_record_mode_starts_fresh(self, path: str) -> None:
        record(path, Wire(), Req({"a": 1}), Req({"a": 2}))
        redo = record(path, Wire(), Req({"a": 3}), mode="record")
        assert redo.size == 1 and Cassette(path).size == 1

    def test_same_request_replays_in_recorded_order(self, path: str) -> None:
        wire = Wire(
            (429, [("retry-after", "2")], [b'{"e": "slow"}']),
            (200, [], [b'{"ok": true}']),
        )
        record(path, wire, Req({"q": 1}), Req({"q": 1}))
        transport = Cassette(path).transport()
        first = transport.handle_request(Req({"q": 1}))
        assert (first.status_code, dict(first.headers)["retry-after"]) == (429, "2")
        assert transport.handle_request(Req({"q": 1})).status_code == 200
        with pytest.raises(CassetteMiss, match="already replayed"):
            transport.handle_request(Req({"q": 1}))

    def test_allow_repeat_serves_the_last_recording_again(self, path: str) -> None:
        record(path, Wire(), Req({"q": 1}))
        transport = Cassette(path, allow_repeat=True).transport()
        for _ in range(3):
            assert transport.handle_request(Req({"q": 1})).status_code == 200

    def test_streamed_chunks_keep_their_boundaries(self, path: str) -> None:
        chunks = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]
        wire = Wire((200, [("content-type", "text/event-stream")], chunks))
        record(path, wire, Req({"stream": True}))
        replayed = Cassette(path).transport().handle_request(Req({"stream": True}))
        assert list(replayed.stream) == chunks

    def test_async_round_trip(self, path: str) -> None:
        wire = Wire()

        async def main() -> bytes:
            cassette = Cassette(path)
            response = await cassette.transport(wire).handle_async_request(Req({"x": 1}))
            async for _ in response.stream:
                pass
            replay = await Cassette(path).transport().handle_async_request(Req({"x": 1}))
            return b"".join([c async for c in replay.stream])

        assert asyncio.run(main()) == b'{"ok": 1}'
        assert len(wire.calls) == 1

    def test_non_text_body_is_base64(self, path: str) -> None:
        blob = bytes(range(256))
        record(path, Wire((200, [], [blob[:100], blob[100:]])), Req({"b": 1}))
        data = json.loads(Path(path).read_text())
        assert data["entries"][0]["response"]["body"]["encoding"] == "base64"
        response = Cassette(path).transport().handle_request(Req({"b": 1}))
        assert read_all(response) == blob

    def test_non_json_request_bodies_match_by_content(self, path: str) -> None:
        record(path, Wire(), Req(raw=b"plain text prompt"))
        transport = Cassette(path).transport()
        assert transport.handle_request(Req(raw=b"plain text prompt")).status_code == 200
        with pytest.raises(CassetteMiss):
            transport.handle_request(Req(raw=b"another"))


class TestMatching:
    def test_json_key_order_does_not_matter(self, path: str) -> None:
        record(path, Wire(), Req({"a": 1, "b": {"c": 2, "d": 3}}))
        reply = Cassette(path).transport().handle_request(Req({"b": {"d": 3, "c": 2}, "a": 1}))
        assert reply.status_code == 200

    def test_different_body_misses_with_a_useful_message(self, path: str) -> None:
        record(path, Wire(), Req({"model": "m", "n": 1}))
        with pytest.raises(CassetteMiss) as info:
            Cassette(path).transport().handle_request(Req({"model": "m", "n": 2}))
        exc = info.value
        assert exc.method == "POST" and "llm.example" in exc.url and len(exc.key) == 64
        assert "nothing like it is recorded" in str(exc) and "mode='record'" in str(exc)

    def test_ignore_body_keys_by_name_and_path(self, path: str) -> None:
        record(
            path,
            Wire(),
            Req({"q": 1, "user": "a", "meta": {"run": 7, "keep": 1}}),
            ignore_body_keys=("user", "meta.run"),
        )
        replay = Cassette(path, ignore_body_keys=("user", "meta.run")).transport()
        assert replay.handle_request(Req({"q": 1, "user": "zzz", "meta": {"run": 9, "keep": 1}}))
        with pytest.raises(CassetteMiss):
            replay.handle_request(Req({"q": 1, "meta": {"run": 7, "keep": 2}}))
        assert '"user"' not in json.dumps(Cassette(path).entries[0]["request"]["body"])

    def test_ignore_query_and_host(self, path: str) -> None:
        options: dict[str, Any] = {"ignore_query": ("trace",), "ignore_host": True}
        record(
            path, Wire(), Req({"a": 1}, url="https://one.example/v1/chat?x=1&trace=aaa"), **options
        )
        replay = Cassette(path, **options).transport()
        assert replay.handle_request(Req({"a": 1}, url="https://two.example/v1/chat?trace=zzz&x=1"))
        strict = Cassette(path).transport()
        with pytest.raises(CassetteMiss):
            strict.handle_request(Req({"a": 1}, url="https://two.example/v1/chat?x=1&trace=aaa"))

    def test_query_order_is_irrelevant(self, path: str) -> None:
        record(path, Wire(), Req({}, url="https://h.example/p?a=1&b=2"))
        assert Cassette(path).transport().handle_request(Req({}, url="https://h.example/p?b=2&a=1"))

    def test_method_and_header_matching(self, path: str) -> None:
        record(
            path,
            Wire(),
            Req({}, headers={"x-model-version": "1"}),
            match_headers=("x-model-version",),
        )
        replay = Cassette(path, match_headers=("x-model-version",)).transport()
        with pytest.raises(CassetteMiss):
            replay.handle_request(Req({}, headers={"x-model-version": "2"}))
        with pytest.raises(CassetteMiss):
            replay.handle_request(Req({}, method="GET", headers={"x-model-version": "1"}))
        assert replay.handle_request(Req({}, headers={"x-model-version": "1"}))

    def test_changing_the_api_key_still_matches(self, path: str) -> None:
        record(path, Wire(), Req({"q": 1}))
        other = Req({"q": 1}, headers={"authorization": "Bearer sk-different-0000000000000000"})
        assert Cassette(path).transport().handle_request(other)


class TestSecrets:
    def test_nothing_secret_reaches_the_file(self, path: str) -> None:
        body = {
            "model": "m",
            "api_key": "body-key-123456",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": f"my key is {SECRET}"}],
            "nested": {"password": "hunter2hunter2", "auth": {"token": "t"}},
        }
        request = Req(
            body,
            url=f"https://llm.example/v1?key=query-key-98765&api_key=zzz-secret&x=1&echo={SECRET}",
            headers={
                "authorization": f"Bearer {SECRET}",
                "x-api-key": "header-key-555555",
                "cookie": "session=abc123",
                "user-agent": "tests",
                "x-custom": "contains literal-secret-value inside",
            },
        )
        wire = Wire(
            (
                200,
                [("set-cookie", "sid=abcdef123456"), ("content-type", "application/json")],
                [json.dumps({"echo": SECRET, "usage": {"input_tokens": 5}}).encode()],
            )
        )
        record(path, wire, request, secrets=("literal-secret-value", "ab"))
        text = Path(path).read_text()
        for leaked in (
            SECRET, "body-key-123456", "hunter2hunter2", "header-key-555555", "abc123",
            "query-key-98765", "zzz-secret", "literal-secret-value", "abcdef123456",
        ):  # fmt: skip
            assert leaked not in text, leaked
        data = json.loads(text)["entries"][0]
        assert "max_tokens" in text and data["request"]["body"]["json"]["max_tokens"] == 50
        assert "input_tokens" in data["response"]["body"]["chunks"][0]
        assert data["request"]["body"]["json"]["api_key"] == "***"
        assert dict(map(tuple, data["request"]["headers"]))["user-agent"] == "tests"  # type: ignore[arg-type]
        assert "x=1" in data["request"]["url"]

    def test_secret_straddling_stream_chunks_is_masked(self, path: str) -> None:
        wire = Wire((200, [], [f"data: {SECRET[:12]}".encode(), f"{SECRET[12:]}\n\n".encode()]))
        record(path, wire, Req({"s": 1}))
        assert SECRET not in Path(path).read_text()

    def test_custom_redactor_replaces_the_default(self, path: str) -> None:
        def redactor(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    k: ("<gone>" if k in ("prompt", "authorization") else v) for k, v in obj.items()
                }
            return obj

        record(path, Wire(), Req({"prompt": "private text", "n": 1}), redactor=redactor)
        text = Path(path).read_text()
        assert "private text" not in text and "<gone>" in text
        replay = Cassette(path, redactor=redactor).transport()
        assert replay.handle_request(Req({"prompt": "different private text", "n": 1}))

    def test_response_redaction_is_opt_in(self, path: str) -> None:
        wire = Wire((200, [], [b'{"password": "pw-123456", "usage": {"output_tokens": 3}}']))
        record(path, wire, Req({"a": 1}), redact_responses=True)
        text = Path(path).read_text()
        assert "pw-123456" not in text and "output_tokens" in text

    def test_file_is_private(self, path: str) -> None:
        record(path, Wire(), Req({}))
        assert os.stat(path).st_mode & 0o077 == 0


class TestFailures:
    def test_truncated_file(self, path: str) -> None:
        record(path, Wire(), Req({"a": 1}), Req({"a": 2}))
        raw = Path(path).read_bytes()
        Path(path).write_bytes(raw[: len(raw) // 2])
        with pytest.raises(CassetteError, match="truncated or corrupt"):
            Cassette(path)

    @pytest.mark.parametrize(
        "content",
        [b"", b"[]", b'{"version": 99, "entries": []}', b'{"version": 1}', b"\xff\xfe\x00"],
    )
    def test_malformed_files(self, path: str, content: bytes) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(content)
        with pytest.raises(CassetteError):
            Cassette(path)

    def test_damaged_entry(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"version": 1, "entries": [{"key": "k"}]}))
        with pytest.raises(CassetteError, match="damaged entry"):
            Cassette(path)

    def test_replay_mode_needs_the_file(self, path: str) -> None:
        with pytest.raises(CassetteError, match="does not exist"):
            Cassette(path, mode="replay")

    def test_record_mode_ignores_a_corrupt_file(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("{not json")
        assert Cassette(path, mode="record").size == 0

    def test_failed_save_leaves_the_old_file_intact(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record(path, Wire(), Req({"a": 1}))
        before = Path(path).read_bytes()
        cassette = Cassette(path, mode="new")
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            cassette.transport(Wire()).handle_request(Req({"a": 2})).read()
        monkeypatch.undo()
        assert Path(path).read_bytes() == before
        assert not [p for p in Path(path).parent.iterdir() if p.name.endswith(".tmp")]

    def test_incomplete_stream_is_flagged_and_fails_loudly_on_replay(self, path: str) -> None:
        wire = Wire((200, [], [b"one", b"two", b"three"]))
        transport = Cassette(path).transport(wire)
        response = transport.handle_request(Req({"s": 1}))
        stream = iter(response.stream)
        assert next(stream) == b"one"
        response.stream.close()  # the caller walked away early
        assert Cassette(path).entries[0]["response"]["complete"] is False
        replayed = Cassette(path).transport().handle_request(Req({"s": 1}))
        with pytest.raises(CassetteError, match="closed before it finished"):
            list(replayed.stream)

    def test_unused_recordings_can_be_asserted(self, path: str) -> None:
        record(path, Wire(), Req({"a": 1}), Req({"a": 2}))
        replay = Cassette(path)
        replay.transport().handle_request(Req({"a": 1}))
        with pytest.raises(AssertionError, match="never replayed"):
            replay.assert_all_replayed()

    def test_new_mode_replays_known_and_records_unknown(self, path: str) -> None:
        record(path, Wire(), Req({"a": 1}))
        wire = Wire((201, [], [b"fresh"]))
        cassette = Cassette(path, mode="new")
        transport = cassette.transport(wire)
        assert transport.handle_request(Req({"a": 1})).status_code == 200
        assert not wire.calls
        assert read_all(transport.handle_request(Req({"a": 2}))) == b"fresh"
        assert cassette.size == 2 and len(wire.calls) == 1

    def test_bad_mode(self, path: str) -> None:
        with pytest.raises(ValueError):
            Cassette(path, mode="always")  # type: ignore[arg-type]

    def test_recording_without_inner_and_without_httpx_explains(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import python_wrap.llm_cassette as mod

        monkeypatch.setattr(mod, "_httpx", lambda: None)
        with pytest.raises(RuntimeError, match="inner transport"):
            Cassette(path).transport().handle_request(Req({}))

    def test_context_manager_saves_when_autosave_is_off(self, path: str) -> None:
        with Cassette(path, autosave=False) as cassette:
            read_all(cassette.transport(Wire()).handle_request(Req({"a": 1})))
            assert not os.path.exists(path)
        assert os.path.exists(path)

    def test_concurrent_recording_keeps_every_entry(self, path: str) -> None:
        from concurrent.futures import ThreadPoolExecutor

        cassette = Cassette(path)
        transport = cassette.transport(Wire())

        def one(i: int) -> None:
            read_all(transport.handle_request(Req({"i": i})))

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(one, range(40)))
        assert cassette.size == 40 and Cassette(path).size == 40


# ------------------------------------------------------------------------- properties

json_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(-1000, 1000), st.text(max_size=12, alphabet="abcdef xyz")
)
json_values = st.recursive(
    json_scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=6, alphabet="abcxyz_"), inner, max_size=4),
    ),
    max_leaves=12,
)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    body=st.dictionaries(
        st.text(min_size=1, max_size=6, alphabet="abcxyz_"), json_values, max_size=5
    ),
    chunks=st.lists(st.binary(max_size=40), min_size=1, max_size=5),
    status=st.sampled_from([200, 201, 400, 429, 500, 529]),
)
def test_property_round_trip(
    tmp_path_factory: pytest.TempPathFactory, body: dict[str, Any], chunks: list[bytes], status: int
) -> None:
    path = str(tmp_path_factory.mktemp("rt") / "c.json")
    record(path, Wire((status, [("x-trace", "t")], chunks)), Req(body))
    response = Cassette(path).transport().handle_request(Req(body))
    assert response.status_code == status
    assert read_all(response) == b"".join(chunks)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    secret=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=10, max_size=24),
    where=st.sampled_from(["header", "body", "query", "response", "all"]),
)
def test_property_declared_secrets_are_never_stored(
    tmp_path_factory: pytest.TempPathFactory, secret: str, where: str
) -> None:
    path = str(tmp_path_factory.mktemp("sec") / "c.json")
    in_body = {"note": f"value {secret} end"} if where in ("body", "all") else {"note": "x"}
    url = f"https://h.example/p?z={secret}" if where in ("query", "all") else "https://h.example/p"
    headers = {"x-trace": secret} if where in ("header", "all") else {"x-trace": "t"}
    reply = f'{{"echo": "{secret}"}}'.encode() if where in ("response", "all") else b"{}"
    record(
        path,
        Wire((200, [("x-out", secret)] if where in ("response", "all") else [], [reply])),
        Req(in_body, url=url, headers=headers),
        secrets=(secret,),
    )
    text = Path(path).read_text()
    assert secret not in text
    # and replay still works with the secret present in the new request
    assert (
        Cassette(path, secrets=(secret,))
        .transport()
        .handle_request(Req(in_body, url=url, headers=headers))
    )


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    bodies=st.lists(st.integers(0, 6), min_size=1, max_size=12),
)
def test_property_replay_order_is_per_key(
    tmp_path_factory: pytest.TempPathFactory, bodies: list[int]
) -> None:
    path = str(tmp_path_factory.mktemp("ord") / "c.json")
    counter = dict.fromkeys(range(7), 0)
    replies = []
    for b in bodies:
        counter[b] += 1
        replies.append((200, [], [f"{b}:{counter[b]}".encode()]))
    cassette = Cassette(path)
    seq = iter(replies)

    class Seq:
        def handle_request(self, request: Any) -> Any:
            status, headers, chunks = next(seq)
            return build_response(request, status, headers, chunks, asynchronous=False)

    transport = cassette.transport(Seq())
    for b in bodies:
        read_all(transport.handle_request(Req({"b": b})))
    replay = Cassette(path).transport()
    seen = dict.fromkeys(range(7), 0)
    for b in bodies:
        seen[b] += 1
        assert read_all(replay.handle_request(Req({"b": b}))) == f"{b}:{seen[b]}".encode()
