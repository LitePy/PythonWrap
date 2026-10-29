"""Framework contracts, with optional real-framework tests in the integration job."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from python_wrap.deadline import remaining
from python_wrap.fastapi import ContextMiddleware
from python_wrap.logs import current_log_context


@pytest.mark.asyncio
async def test_asgi_context_lifetime_deadline_streaming_and_error():
    seen = []

    async def app(scope, receive, send):
        seen.append(current_log_context())
        assert remaining() is not None and remaining() <= 5
        await asyncio.sleep(0)
        seen.append(current_log_context())
        raise RuntimeError("view error")

    middleware = ContextMiddleware(app, budget=5, user=lambda scope: "user")
    with pytest.raises(RuntimeError, match="view error"):
        await middleware({"type": "http", "headers": [(b"x-request-id", b"test-id")]}, None, None)
    assert seen == [{"request_id": "test-id", "user": "user"}] * 2
    assert current_log_context() == {} and remaining() is None


@pytest.mark.asyncio
async def test_asgi_non_http_and_concurrent_request_isolation():
    async def app(scope, receive, send):
        await asyncio.sleep(0)
        send.append(current_log_context())

    middleware = ContextMiddleware(app)
    a, b = [], []
    await asyncio.gather(
        middleware({"type": "http", "headers": [(b"x-request-id", b"a")]}, None, a),
        middleware({"type": "http", "headers": [(b"x-request-id", b"b")]}, None, b),
    )
    assert a == [{"request_id": "a"}] and b == [{"request_id": "b"}]
    out = []
    await middleware({"type": "lifespan"}, None, out)
    assert out == [{}]


def test_django_sync_dispatch_context_restores_after_exception():
    from python_wrap.django import ContextMiddleware as DjangoMiddleware

    def view(request):
        assert current_log_context() == {"request_id": "r1", "user": "7"}
        raise ValueError("view")

    request = SimpleNamespace(
        META={"HTTP_X_REQUEST_ID": "r1"}, user=SimpleNamespace(is_authenticated=True, pk=7)
    )
    with pytest.raises(ValueError):
        DjangoMiddleware(view)(request)
    assert current_log_context() == {}


def test_real_flask_request_teardown():
    flask = pytest.importorskip("flask")
    from python_wrap.flask import init_app

    app = flask.Flask(__name__)
    init_app(app, user=lambda: "ana", budget=5)

    @app.route("/")
    def view():
        return current_log_context()

    client = app.test_client()
    assert client.get("/", headers={"X-Request-ID": "r1"}).json == {
        "user": "ana",
        "request_id": "r1",
    }
    assert current_log_context() == {}


def test_real_fastapi_requests():
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from python_wrap.fastapi import install

    app = fastapi.FastAPI()
    install(app, budget=5, health_path="/health/dependencies")

    @app.get("/")
    def index():
        return current_log_context()

    with TestClient(app) as client:
        assert client.get("/", headers={"X-Request-ID": "r2"}).json()["request_id"] == "r2"
        assert client.get("/health/dependencies").status_code == 200
    assert current_log_context() == {}


def test_real_celery_eager_task_context_and_disconnect():
    celery = pytest.importorskip("celery")
    from python_wrap.celery import install

    app = celery.Celery("pythonwrap-test", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    handle = install(app)

    @app.task(name="test_context")
    def task():
        return current_log_context()

    result = task.delay()
    data = result.get()
    assert data["task_id"] == result.id and data["task_name"] == "test_context"
    assert current_log_context() == {}
    handle.close()
    assert task.delay().get() == {}
    app.close()


@pytest.mark.asyncio
async def test_real_django_async_middleware():
    pytest.importorskip("django")
    from asgiref.sync import iscoroutinefunction

    from python_wrap.django import ContextMiddleware as DjangoMiddleware

    async def view(request):
        return current_log_context()

    middleware = DjangoMiddleware(view)
    assert iscoroutinefunction(middleware)
    assert await middleware(SimpleNamespace(META={"HTTP_X_REQUEST_ID": "r3"})) == {
        "request_id": "r3"
    }
    assert current_log_context() == {}


def test_real_flask_nested_requests_do_not_lose_outer_context():
    flask = pytest.importorskip("flask")
    from python_wrap.flask import init_app

    app = flask.Flask(__name__)
    init_app(app)

    @app.route("/inner")
    def inner():
        return current_log_context()

    @app.route("/outer")
    def outer():
        before = dict(current_log_context())
        nested = app.test_client().get("/inner", headers={"X-Request-ID": "inner"}).json
        return {"before": before, "inner": nested, "after": current_log_context()}

    result = app.test_client().get("/outer", headers={"X-Request-ID": "outer"}).json
    assert result == {
        "before": {"request_id": "outer"},
        "inner": {"request_id": "inner"},
        "after": {"request_id": "outer"},
    }
    assert current_log_context() == {}


def test_real_celery_close_inside_task_still_restores_context():
    celery = pytest.importorskip("celery")
    from python_wrap.celery import install

    app = celery.Celery("pythonwrap-close-test", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    handle = install(app)

    @app.task(name="close_context")
    def task():
        handle.close()
        return current_log_context()

    result = task.delay()
    assert result.get()["task_id"] == result.id
    assert current_log_context() == {}
    app.close()


@pytest.mark.asyncio
async def test_real_django_async_user_resolution_uses_auser_without_sync_lazy_access():
    pytest.importorskip("django")
    from python_wrap.django import ContextMiddleware as DjangoMiddleware

    class Request:
        def __init__(self):
            self.META = {"HTTP_X_REQUEST_ID": "id"}

        @property
        def user(self):
            raise AssertionError("sync user access in async request")

        async def auser(self):
            return SimpleNamespace(is_authenticated=True, pk=9)

    async def view(request):
        return current_log_context()

    assert await DjangoMiddleware(view)(Request()) == {"request_id": "id", "user": "9"}
    assert current_log_context() == {}


@pytest.mark.asyncio
async def test_asgi_cancellation_restores_context_and_cooperative_deadline():
    started = asyncio.Event()

    async def app(scope, receive, send):
        started.set()
        await asyncio.Event().wait()

    middleware = ContextMiddleware(app, budget=1)

    async def call():
        try:
            await middleware({"type": "http", "headers": []}, None, None)
        finally:
            assert current_log_context() == {} and remaining() is None

    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_django_recognizes_marked_coroutine_callable():
    pytest.importorskip("django")
    from asgiref.sync import markcoroutinefunction

    from python_wrap.django import ContextMiddleware as DjangoMiddleware

    class Handler:
        def __call__(self, request):
            async def response():
                return current_log_context()

            return response()

    handler = Handler()
    markcoroutinefunction(handler)
    middleware = DjangoMiddleware(handler)
    assert middleware.is_async
    assert await middleware(SimpleNamespace(META={"HTTP_X_REQUEST_ID": "marked"})) == {
        "request_id": "marked"
    }
