"""Record and replay LLM HTTP traffic so tests run offline, fast and deterministically.

A `Cassette` sits at the HTTP transport level, below any provider SDK: build the SDK's
``httpx`` client with ``transport=cassette.transport()`` (or wrap a transport you already
have) and the first run talks to the real API and writes a JSON file; later runs replay
it with no network and no key. The classes are duck-typed against the ``httpx`` transport
protocol (``handle_request`` / ``handle_async_request`` taking a request with ``method``,
``url``, ``headers`` and ``read()``/``content``); httpx is only imported lazily, to build
real ``httpx.Response`` objects, and a minimal stand-in is used when it is absent.

What a cassette guarantees:

- **Secrets are never stored.** Authorization-style headers, sensitive JSON keys,
  sensitive query parameters, the literal values in ``secrets=`` and well-known key
  shapes (bearer tokens, ``sk-...``, cloud keys) are masked before anything is written,
  and the request key is computed from the masked form, so a changed API key still
  matches.
- **Requests match by hash** of method, URL (query sorted) and JSON body (keys sorted),
  after dropping ``ignore_body_keys`` / ``ignore_query``. Repeated identical requests
  replay in recorded order (a 429 then a 200 replays as a 429 then a 200).
- **Streams keep their chunks**: a streamed (server-sent events) body is recorded chunk
  by chunk as it is consumed and replayed with the same boundaries.
- **Strict replay**: an unrecorded request raises `CassetteMiss` instead of reaching the
  network. A truncated or corrupt file raises `CassetteError`; it is never half-used.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib
import json
import os
import re
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from types import TracebackType
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, quote, urlsplit

from python_wrap import _core
from python_wrap.errors import PythonWrapError
from python_wrap.redaction import MASK, is_sensitive, redact

__all__ = [
    "SECRET_FRAGMENTS",
    "Cassette",
    "CassetteError",
    "CassetteMiss",
    "RecordingTransport",
    "ReplayTransport",
]

CassetteMode = Literal["once", "record", "replay", "new"]
_MODES = ("once", "record", "replay", "new")
_VERSION = 1

#: Name fragments treated as secret in headers, JSON keys and query parameters. Unlike
#: ``redaction.DEFAULT_REDACT`` it leaves ``tokens`` alone, because LLM requests and
#: responses are full of ``max_tokens`` and ``input_tokens`` that must stay readable.
SECRET_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "proxy_authorization",
    "credential",
    "private_key",
    "cookie",
    "set_cookie",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "bearer",
    "x_api_key",
    "signature",
)

#: Query parameters are stricter: a bare ``key``, ``token``, ``sig`` or ``auth`` is a secret there.
_QUERY_FRAGMENTS = (*SECRET_FRAGMENTS, "key", "token", "sig", "auth")

_SECRET_TEXT = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
)
_DROP_ON_REPLAY = frozenset({"content-length", "transfer-encoding"})
_IGNORED = object()


class CassetteError(PythonWrapError):
    """A cassette file is unreadable, truncated or in an unknown format."""


class CassetteMiss(CassetteError):
    """A request has no (unused) recording, and the cassette is not recording.

    Attributes:
        method: The request method.
        url: The request URL with secrets masked.
        key: The request's match key (hex digest).
    """

    def __init__(self, message: str, *, method: str, url: str, key: str) -> None:
        super().__init__(message)
        self.method, self.url, self.key = method, url, key


# ------------------------------------------------------------------------ duck typing


def _httpx() -> Any:
    try:
        return importlib.import_module("httpx")
    except ImportError:
        return None


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    """Every ``(name, value)`` of a headers object, repeated names included."""
    if headers is None:
        return []
    multi = getattr(headers, "multi_items", None)
    items: Iterable[Any] = cast("Iterable[Any]", multi()) if callable(multi) else _items(headers)
    pairs: list[tuple[str, str]] = []
    for key, value in items:
        if isinstance(key, bytes):
            key = key.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        pairs.append((str(key), str(value)))
    return pairs


def _items(headers: Any) -> Iterable[Any]:
    return cast("Iterable[Any]", headers.items() if hasattr(headers, "items") else headers)


def _body_of(request: Any) -> bytes:
    reader = getattr(request, "read", None)
    data = reader() if callable(reader) else getattr(request, "content", b"")
    return _as_bytes(data)


async def _abody_of(request: Any) -> bytes:
    reader = getattr(request, "aread", None)
    if callable(reader):
        return _as_bytes(await reader())
    return _body_of(request)


def _as_bytes(data: Any) -> bytes:
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


class _PlainResponse:
    """The stand-in response used when httpx is not installed."""

    def __init__(self, status_code: int, headers: list[tuple[str, str]], stream: Any, request: Any):
        self.status_code = status_code
        self.headers = dict(headers)
        self.stream = stream
        self.request = request
        self._content: bytes | None = None

    @property
    def content(self) -> bytes:
        if self._content is None:
            self._content = b"".join(self.stream)
        return self._content

    def read(self) -> bytes:
        return self.content

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self.stream

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if callable(close):
            close()


class _Chunks:
    """A replayable chunk list usable as a sync or an async byte stream."""

    def __init__(self, chunks: Sequence[bytes], then: BaseException | None = None) -> None:
        self._chunks, self._then = list(chunks), then

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._then is not None:
            raise self._then

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._then is not None:
            raise self._then

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def build_response(
    request: Any,
    status: int,
    headers: Sequence[tuple[str, str]],
    chunks: Sequence[bytes],
    *,
    asynchronous: bool,
    then: BaseException | None = None,
) -> Any:
    """Make a response object (``httpx.Response`` when httpx is installed) from recorded parts."""
    kept = [(k, v) for k, v in headers if k.lower() not in _DROP_ON_REPLAY]
    mod = _httpx()
    if mod is None:
        return _PlainResponse(status, kept, _Chunks(chunks, then), request)
    body = _Chunks(chunks, then)
    content: Any = body.__aiter__() if asynchronous else iter(body)
    if then is None and not asynchronous:
        content = b"".join(chunks) if len(chunks) <= 1 else iter(body)
    return mod.Response(status, headers=kept, content=content, request=request)


# -------------------------------------------------------------------------- recording


class _Tee:
    """Passes a response's byte stream through while remembering its chunks."""

    def __init__(self, inner: Any, finish: Callable[[list[bytes], bool], None]) -> None:
        self._inner, self._finish = inner, finish
        self._chunks: list[bytes] = []
        self._complete = False
        self._done = False
        self._lock = threading.Lock()

    def _settle(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._finish(self._chunks, self._complete)

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._inner:
                self._chunks.append(bytes(chunk))
                yield chunk
            self._complete = True
        finally:
            self._settle()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                self._chunks.append(bytes(chunk))
                yield chunk
            self._complete = True
        finally:
            self._settle()

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if callable(close):
                close()
        finally:
            self._settle()

    async def aclose(self) -> None:
        try:
            aclose = getattr(self._inner, "aclose", None)
            if callable(aclose):
                await aclose()
        finally:
            self._settle()


def _tee_for(inner: Any, finish: Callable[[list[bytes], bool], None]) -> Any:
    """A `_Tee` that is also an instance of httpx's stream base classes when present."""
    return stream_class(_Tee)(inner, finish)


_STREAM_CLASSES: dict[tuple[type, int], type] = {}


def stream_class(impl: type) -> type:
    """``impl`` extended with httpx's sync and async byte-stream bases, when httpx exists.

    httpx checks ``isinstance(response.stream, SyncByteStream)``, so a stream wrapper must
    inherit from them; doing it here keeps httpx a lazy, optional import.
    """
    mod = _httpx()
    if mod is None:
        return impl
    found = _STREAM_CLASSES.get((impl, id(mod)))
    if found is None:
        bases = tuple(
            b
            for b in (getattr(mod, "SyncByteStream", None), getattr(mod, "AsyncByteStream", None))
            if isinstance(b, type)
        )
        found = type(f"_Httpx{impl.__name__}", (impl, *bases), {})
        _STREAM_CLASSES[(impl, id(mod))] = found
    return found


# ---------------------------------------------------------------------------- cassette


def _mask_text(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return _SECRET_TEXT.sub(MASK, text)


def _clean(value: Any, secrets: Sequence[str]) -> Any:
    """Mask secret-looking text in every string of a JSON value (keys untouched)."""
    if isinstance(value, str):
        return _mask_text(value, secrets)
    if isinstance(value, Mapping):
        return {k: _clean(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, secrets) for v in value]
    return value


def _drop_keys(value: Any, names: frozenset[str], paths: frozenset[str], path: str = "") -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) in names or here in paths:
                continue
            out[key] = _drop_keys(item, names, paths, here)
        return out
    if isinstance(value, list):
        return [_drop_keys(item, names, paths, path) for item in value]
    return value


def _encode_body(chunks: Sequence[bytes], secrets: Sequence[str]) -> dict[str, Any]:
    """The cassette form of a body: masked UTF-8 text, or base64 when it is not text."""
    try:
        texts = [chunk.decode("utf-8") for chunk in chunks]
    except UnicodeDecodeError:
        try:
            texts = [b"".join(chunks).decode("utf-8")]
        except UnicodeDecodeError:
            return {
                "encoding": "base64",
                "chunks": [base64.b64encode(chunk).decode("ascii") for chunk in chunks],
            }
    joined = "".join(texts)
    masked = _mask_text(joined, secrets)
    if masked != joined:  # a secret may straddle chunks: keep one masked chunk
        texts = [masked]
    return {"encoding": "utf-8", "chunks": texts}


def _decode_body(body: Mapping[str, Any]) -> list[bytes]:
    chunks = body["chunks"]
    if body["encoding"] == "base64":
        return [base64.b64decode(c) for c in chunks]
    if body["encoding"] == "utf-8":
        return [c.encode("utf-8") for c in chunks]
    raise ValueError(f"unknown body encoding {body['encoding']!r}")


class Cassette:
    """A file of recorded HTTP exchanges, used through `transport`.

    Args:
        path: The JSON file (parent directories are created on save).
        mode: ``"once"`` (default) replays when the file exists and records when it does
            not; ``"replay"`` never touches the network (unrecorded requests raise
            `CassetteMiss`); ``"record"`` ignores any existing file and records
            everything; ``"new"`` replays recorded requests and records the rest.
        ignore_body_keys: JSON body keys left out of matching and storage, anywhere in
            the body (``"user"``) or by dotted path from the root (``"metadata.run_id"``).
        ignore_query: Query parameter names left out of matching.
        ignore_host: Match on path and query only, so a base-URL change still replays.
        match_headers: Header names (lowercase) whose values must also match.
        redactor: ``redactor(obj) -> obj`` applied to the request headers mapping and the
            decoded JSON request body before they are stored and hashed; the default masks
            keys named by `SECRET_FRAGMENTS`.
        redact_responses: Apply ``redactor`` to non-streamed JSON response bodies too
            (text-level secret masking always applies).
        secrets: Literal secret values (an API key you hold) masked everywhere.
        allow_repeat: When every recording of a request has been replayed, serve the last
            one again instead of raising `CassetteMiss`.
        autosave: Write the file after every recorded exchange (otherwise on `save` or on
            leaving the ``with`` block).

    Example:
        ```python
        import os
        import tempfile

        from python_wrap.llm_cassette import Cassette

        class Call:  # a stand-in for an httpx request
            method, url, headers = "POST", "https://llm.example/v1/chat", {"x-api-key": "k-123"}
            content = b'{"model": "m", "messages": []}'

        class Wire:  # a stand-in for the real network transport
            def handle_request(self, request):
                from python_wrap.llm_cassette import build_response
                return build_response(request, 200, [("content-type", "application/json")],
                                      [b'{"text": "hi"}'], asynchronous=False)

        path = os.path.join(tempfile.mkdtemp(), "chat.json")
        first = Cassette(path).transport(Wire()).handle_request(Call())
        assert first.read() == b'{"text": "hi"}'
        replay = Cassette(path).transport()  # no network object needed any more
        assert replay.handle_request(Call()).read() == b'{"text": "hi"}'
        assert "k-123" not in open(path).read()
        ```
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        mode: CassetteMode = "once",
        ignore_body_keys: Iterable[str] = (),
        ignore_query: Iterable[str] = (),
        ignore_host: bool = False,
        match_headers: Iterable[str] = (),
        redactor: Callable[[Any], Any] | None = None,
        redact_responses: bool = False,
        secrets: Iterable[str] = (),
        allow_repeat: bool = False,
        autosave: bool = True,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {', '.join(_MODES)}; got {mode!r}")
        self.path = os.fspath(path)
        self.mode = mode
        names: set[str] = set()
        paths: set[str] = set()
        for item in ignore_body_keys:
            (paths if "." in item else names).add(item)
        self._names, self._paths = frozenset(names), frozenset(paths)
        self._ignore_query = frozenset(ignore_query)
        self.ignore_host = ignore_host
        self._match_headers = tuple(h.lower() for h in match_headers)
        self._redactor = redactor
        self.redact_responses = redact_responses
        self._secrets = tuple(s for s in secrets if isinstance(s, str) and len(s) >= 4)
        self.allow_repeat, self.autosave = allow_repeat, autosave
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._used: set[int] = set()
        self.recorded = 0
        self.replayed = 0
        exists = os.path.exists(self.path)
        self.replaying = mode == "replay" or (mode == "once" and exists)
        self.recording = mode in ("record", "new") or (mode == "once" and not exists)
        if mode == "replay" and not exists:
            raise CassetteError(f"cassette {self.path!r} does not exist (mode='replay')")
        if exists and mode != "record":
            self._entries = self._load()

    # -- file -----------------------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CassetteError(
                f"cassette {self.path!r} is truncated or corrupt ({type(exc).__name__}); "
                "re-record it with mode='record'"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != _VERSION
            or not isinstance(data.get("entries"), list)
        ):
            raise CassetteError(f"cassette {self.path!r} has an unknown format")
        entries = data["entries"]
        for entry in entries:
            try:
                _decode_body(entry["response"]["body"])
                int(entry["response"]["status"])
                str(entry["key"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CassetteError(f"cassette {self.path!r} has a damaged entry: {exc}") from exc
        return [dict(e) for e in entries]

    def save(self) -> None:
        """Write the file atomically (temp file, then rename), readable by the owner only."""
        with self._lock:
            payload = json.dumps(
                {"version": _VERSION, "entries": self._entries}, indent=2, ensure_ascii=False
            )
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise

    def __enter__(self) -> Cassette:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.recorded and exc_type is None:
            self.save()

    @property
    def entries(self) -> list[dict[str, Any]]:
        """A deep copy of the recorded exchanges (already free of secrets)."""
        with self._lock:
            return copy.deepcopy(self._entries)

    @property
    def size(self) -> int:
        """How many exchanges the cassette holds."""
        with self._lock:
            return len(self._entries)

    @property
    def unused(self) -> int:
        """How many recorded exchanges have not been replayed."""
        with self._lock:
            return len(self._entries) - len(self._used)

    def assert_all_replayed(self) -> None:
        """Raise `AssertionError` when some recorded exchange was never replayed."""
        if self.unused:
            raise AssertionError(
                f"{self.unused} recorded request(s) in {self.path!r} never replayed"
            )

    # -- describing requests ------------------------------------------------------------
    def _redact(self, obj: Any) -> Any:
        if self._redactor is not None:
            return self._redactor(obj)
        return redact(obj, fragments=SECRET_FRAGMENTS)

    def _url(self, url: str) -> tuple[str, str]:
        """The masked URL to store and the canonical form to hash."""
        parts = urlsplit(url)
        pairs = []
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            if is_sensitive(name, _QUERY_FRAGMENTS, match="segment"):
                value = MASK
            pairs.append((name, _mask_text(value, self._secrets)))
        shown = "&".join(f"{quote(n, safe='')}={quote(v, safe='*')}" for n, v in pairs)
        host = f"{parts.scheme}://{parts.hostname or ''}" + (f":{parts.port}" if parts.port else "")
        stored = f"{host}{parts.path}" + (f"?{shown}" if shown else "")
        keep = sorted((n, v) for n, v in pairs if n not in self._ignore_query)
        canonical = ("" if self.ignore_host else host) + parts.path + repr(keep)
        return _mask_text(stored, self._secrets), canonical

    def _headers(self, pairs: Sequence[tuple[str, str]]) -> list[list[str]]:
        masked = self._redact(dict(pairs))
        out: list[list[str]] = []
        for name, value in pairs:
            hidden = isinstance(masked, Mapping) and masked.get(name) == MASK
            if hidden or is_sensitive(name.replace("-", "_"), SECRET_FRAGMENTS):
                value = MASK
            out.append([name, _mask_text(value, self._secrets)])
        return out

    def describe(
        self, method: str, url: str, headers: Any, body: bytes
    ) -> tuple[str, dict[str, Any]]:
        """The match key and the secret-free stored form of a request."""
        stored_url, canonical_url = self._url(url)
        pairs = _header_pairs(headers)
        shown_headers = self._headers(pairs)
        decoded: Any = _IGNORED
        if body:
            try:
                decoded = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                decoded = _IGNORED
        if decoded is not _IGNORED:
            trimmed = _drop_keys(decoded, self._names, self._paths)
            safe = _clean(self._redact(trimmed), self._secrets)
            stored_body: dict[str, Any] = {"encoding": "json", "json": safe}
            canonical_body: Any = safe
        else:
            stored_body = _encode_body([body], self._secrets)
            canonical_body = stored_body
        wanted = {
            name: next((v for k, v in pairs if k.lower() == name), None)
            for name in self._match_headers
        }
        canonical = json.dumps(
            [method.upper(), canonical_url, canonical_body, wanted],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return key, {
            "method": method.upper(),
            "url": stored_url,
            "headers": shown_headers,
            "body": stored_body,
        }

    def _describe_request(self, request: Any, body: bytes) -> tuple[str, dict[str, Any]]:
        return self.describe(
            str(request.method), str(request.url), getattr(request, "headers", None), body
        )

    # -- recording and replaying ----------------------------------------------------------
    def add(
        self,
        key: str,
        request: dict[str, Any],
        status: int,
        headers: Sequence[tuple[str, str]],
        chunks: Sequence[bytes],
        complete: bool = True,
    ) -> None:
        """Store one exchange (headers and body are masked here) and autosave."""
        shown = self._headers(list(headers))
        body = _encode_body(chunks, self._secrets)
        if self.redact_responses and body["encoding"] == "utf-8" and len(body["chunks"]) == 1:
            with contextlib.suppress(ValueError):
                inner = json.loads(body["chunks"][0])
                body["chunks"] = [json.dumps(self._redact(inner), ensure_ascii=False)]
        entry = {
            "key": key,
            "request": request,
            "response": {
                "status": int(status),
                "headers": shown,
                "body": body,
                "complete": complete,
            },
        }
        with self._lock:
            self._entries.append(entry)
            self.recorded += 1
            if self.autosave:
                self.save()
        _core.emit("llm.cassette_recorded", "cassette", status=int(status), key=key[:12])

    def lookup(self, key: str) -> tuple[int, dict[str, Any]] | None:
        """The next unused recording for ``key`` (or the last one with ``allow_repeat``)."""
        with self._lock:
            last = None
            for index, entry in enumerate(self._entries):
                if entry["key"] != key:
                    continue
                last = index
                if index not in self._used:
                    self._used.add(index)
                    self.replayed += 1
                    return index, entry
            if last is not None and self.allow_repeat:
                self.replayed += 1
                return last, self._entries[last]
        return None

    def knows(self, key: str) -> bool:
        """Whether ``key`` has any recording, used or not."""
        with self._lock:
            return any(e["key"] == key for e in self._entries)

    def transport(self, inner: Any = None) -> Any:
        """A transport for the cassette's mode.

        Args:
            inner: The real transport to record from (``httpx.HTTPTransport()`` when
                omitted and httpx is installed). Not needed when only replaying.

        Returns:
            A `ReplayTransport` or a `RecordingTransport`, to pass as ``transport=`` to
            an ``httpx`` client (sync or async) or to the provider SDK's ``http_client``.
        """
        if self.replaying and self.mode != "new":
            return ReplayTransport(self)
        if self.mode == "new":
            return ReplayTransport(self, fallback=RecordingTransport(self, inner))
        return RecordingTransport(self, inner)


# ----------------------------------------------------------------------- transports


class _DefaultWire:
    """httpx's real transports, created on first use."""

    def __init__(self) -> None:
        self._sync: Any = None
        self._async: Any = None

    def handle_request(self, request: Any) -> Any:
        if self._sync is None:
            self._sync = _require_httpx().HTTPTransport()
        return self._sync.handle_request(request)

    async def handle_async_request(self, request: Any) -> Any:
        if self._async is None:
            self._async = _require_httpx().AsyncHTTPTransport()
        return await self._async.handle_async_request(request)

    def close(self) -> None:
        if self._sync is not None:
            self._sync.close()

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()


def _require_httpx() -> Any:
    mod = _httpx()
    if mod is None:
        raise RuntimeError("recording needs an inner transport (httpx is not installed)")
    return mod


def _force_identity(request: Any) -> None:
    """Ask for an uncompressed body so the cassette can mask secrets in it."""
    headers = getattr(request, "headers", None)
    with contextlib.suppress(Exception):
        if headers is not None:
            headers["accept-encoding"] = "identity"


class TransportContext:
    """``with`` / ``async with`` support and closing for transports that wrap another.

    httpx enters its transport when a client is entered, so every wrapper forwards the
    context to the transport it wraps (``_wrapped``).
    """

    def _wrapped(self) -> Any:
        return None

    def __enter__(self) -> Any:
        inner = self._wrapped()
        if inner is not None and hasattr(inner, "__enter__"):
            inner.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        inner = self._wrapped()
        if inner is not None and hasattr(inner, "__exit__"):
            inner.__exit__(exc_type, exc, tb)
        elif inner is not None and callable(getattr(inner, "close", None)):
            inner.close()

    async def __aenter__(self) -> Any:
        inner = self._wrapped()
        if inner is not None and hasattr(inner, "__aenter__"):
            await inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        inner = self._wrapped()
        if inner is not None and hasattr(inner, "__aexit__"):
            await inner.__aexit__(exc_type, exc, tb)
        elif inner is not None and callable(getattr(inner, "aclose", None)):
            await inner.aclose()


class RecordingTransport(TransportContext):
    """Forwards requests to a real transport and records each exchange in a `Cassette`.

    Streamed bodies are passed through as they arrive and written to the cassette when the
    stream ends or is closed (an early close is recorded as incomplete). Requests are sent
    with ``accept-encoding: identity`` so recorded bodies are readable text.

    Args:
        cassette: Where to record.
        inner: The transport to forward to; httpx's default transports when omitted.
    """

    def __init__(self, cassette: Cassette, inner: Any = None) -> None:
        self.cassette = cassette
        self.inner = inner if inner is not None else _DefaultWire()

    def _wrap(self, request: Any, response: Any, key: str, described: dict[str, Any]) -> Any:
        headers = _header_pairs(getattr(response, "headers", None))
        status = int(response.status_code)
        if getattr(response, "is_stream_consumed", False) and hasattr(response, "content"):
            kept = [(k, v) for k, v in headers if k.lower() != "content-encoding"]
            self.cassette.add(key, described, status, kept, [_as_bytes(response.content)], True)
            return response

        def finish(chunks: list[bytes], complete: bool) -> None:
            self.cassette.add(key, described, status, headers, chunks, complete)

        response.stream = _tee_for(response.stream, finish)
        return response

    def _wrapped(self) -> Any:
        return self.inner

    def handle_request(self, request: Any) -> Any:
        """Forward one request (sync) and record the exchange."""
        key, described = self.cassette._describe_request(request, _body_of(request))
        _force_identity(request)
        response = self.inner.handle_request(request)
        return self._wrap(request, response, key, described)

    async def handle_async_request(self, request: Any) -> Any:
        """Forward one request (async) and record the exchange."""
        key, described = self.cassette._describe_request(request, await _abody_of(request))
        _force_identity(request)
        response = await self.inner.handle_async_request(request)
        return self._wrap(request, response, key, described)

    def close(self) -> None:
        """Close the inner transport."""
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        """Close the inner transport (async)."""
        aclose = getattr(self.inner, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            self.close()


class ReplayTransport(TransportContext):
    """Answers requests from a `Cassette` without any network access.

    A request with no unused recording raises `CassetteMiss` (strict replay), unless a
    ``fallback`` transport was given (the ``"new"`` mode), which then handles and records it.
    A recorded stream that was closed early replays its chunks and then raises
    `CassetteError` if the caller reads further.

    Args:
        cassette: What to replay.
        fallback: A `RecordingTransport` for unrecorded requests; none means fail.
    """

    def __init__(self, cassette: Cassette, fallback: RecordingTransport | None = None) -> None:
        self.cassette = cassette
        self.fallback = fallback

    def _miss(self, request: Any, key: str, described: dict[str, Any]) -> CassetteMiss:
        seen = self.cassette.knows(key)
        why = (
            "every recording of it was already replayed"
            if seen
            else f"nothing like it is recorded ({self.cassette.size} exchange(s) in the file)"
        )
        return CassetteMiss(
            f"cassette {self.cassette.path!r}: {described['method']} {described['url']} "
            f"(key {key[:12]}) cannot be served: {why}. Re-record with mode='record' "
            "or 'new' if the request legitimately changed.",
            method=described["method"],
            url=described["url"],
            key=key,
        )

    def _serve(self, request: Any, key: str, described: dict[str, Any], asynchronous: bool) -> Any:
        found = self.cassette.lookup(key)
        if found is None:
            return None
        recorded = found[1]["response"]
        headers = [(str(k), str(v)) for k, v in recorded["headers"]]
        then = (
            None
            if recorded.get("complete", True)
            else CassetteError("the recorded stream was closed before it finished")
        )
        chunks = _decode_body(recorded["body"])
        return build_response(
            request, int(recorded["status"]), headers, chunks, asynchronous=asynchronous, then=then
        )

    def _wrapped(self) -> Any:
        return self.fallback

    def handle_request(self, request: Any) -> Any:
        """Replay one request (sync)."""
        key, described = self.cassette._describe_request(request, _body_of(request))
        response = self._serve(request, key, described, False)
        if response is not None:
            return response
        if self.fallback is not None:
            return self.fallback.handle_request(request)
        raise self._miss(request, key, described)

    async def handle_async_request(self, request: Any) -> Any:
        """Replay one request (async)."""
        key, described = self.cassette._describe_request(request, await _abody_of(request))
        response = self._serve(request, key, described, True)
        if response is not None:
            return response
        if self.fallback is not None:
            return await self.fallback.handle_async_request(request)
        raise self._miss(request, key, described)

    def close(self) -> None:
        """Close the fallback transport, if any."""
        if self.fallback is not None:
            self.fallback.close()

    async def aclose(self) -> None:
        """Close the fallback transport, if any (async)."""
        if self.fallback is not None:
            await self.fallback.aclose()
