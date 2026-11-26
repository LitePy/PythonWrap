"""Pass a trusted inbound request id and deadline to an outbound HTTP call, offline."""

import asyncio
from types import SimpleNamespace

from python_wrap.fastapi import ContextMiddleware
from python_wrap.http import HttpPolicy, PolicyTransport


class FakeTransport:
    def handle_request(self, request):
        return SimpleNamespace(status_code=200, headers=dict(request.headers))


async def app(scope, receive, send):
    request = SimpleNamespace(
        url=SimpleNamespace(host="service"), method="GET", headers={}, extensions={}
    )
    response = PolicyTransport(FakeTransport(), HttpPolicy(propagate=True)).handle_request(request)
    send.append(response.headers)


async def main():
    sent = []
    middleware = ContextMiddleware(app, budget=30, trust_deadline_header=True)
    await middleware(
        {
            "type": "http",
            "headers": [(b"x-request-id", b"example-1"), (b"x-request-deadline", b"1500")],
        },
        None,
        sent,
    )
    assert sent[0]["X-Request-ID"] == "example-1"
    assert 0 < int(sent[0]["X-Request-Deadline"]) <= 1500
    print(sent[0])


if __name__ == "__main__":
    asyncio.run(main())
