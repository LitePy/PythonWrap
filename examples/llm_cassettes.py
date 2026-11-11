"""Shared limits, validated output and HTTP cassettes, all offline.

Three things in one script, with a fake network standing in for a model API:

1. Two "workers" share one tokens-per-minute bucket in a SQLite file, and a 429's
   rate-limit headers make both back off.
2. ``validated`` repairs a truncated JSON reply with an error-only re-ask.
3. A ``Cassette`` records the exchange once and replays it with no network.

Run: python examples/llm_cassettes.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from python_wrap.chaos import FaultInjector
from python_wrap.errors import RateLimitExceeded
from python_wrap.llm import token_rate_limit
from python_wrap.llm_cassette import Cassette, build_response
from python_wrap.llm_http_chaos import LLMHTTPFaults
from python_wrap.llm_shared import SQLiteTokenWindow
from python_wrap.llm_validate import validated
from python_wrap.testing import fake_clock


class Request:
    """What an HTTP client hands a transport (the few attributes cassettes read)."""

    method = "POST"
    url = "https://llm.example/v1/chat"

    def __init__(self, prompt: str) -> None:
        self.headers = {"authorization": "Bearer sk-example-0123456789abcdef"}
        self.content = json.dumps({"model": "demo", "prompt": prompt}).encode()


class FakeNetwork:
    """The 'real' API: counts how often it is really called."""

    def __init__(self) -> None:
        self.calls = 0

    def handle_request(self, request: Any) -> Any:
        self.calls += 1
        body = json.dumps({"text": '{"city": "Oslo"}', "usage": {"input_tokens": 9}})
        return build_response(
            request,
            200,
            [("content-type", "application/json")],
            [body.encode()],
            asynchronous=False,
        )


def shared_limits(folder: str) -> None:
    window_file = os.path.join(folder, "tokens.db")
    worker_a = token_rate_limit(
        6000, estimate=100, block=False, store=SQLiteTokenWindow(window_file)
    )
    worker_b = token_rate_limit(
        6000, estimate=100, block=False, store=SQLiteTokenWindow(window_file)
    )
    with fake_clock() as clock:
        # Worker A hits a 429 whose headers say "wait 30 seconds": worker B hears about it too.
        worker_a.observe_headers({"retry-after": "30", "x-ratelimit-remaining-tokens": "0"})
        try:
            worker_b(lambda: "sent")()
        except RateLimitExceeded as exc:
            assert 29 < exc.retry_after <= 30
        clock.advance(30)
        assert worker_b(lambda: "sent")() == "sent"


def repaired_output() -> None:
    replies = iter(['{"city": "Os', '{"city": "Oslo"}'])  # cut off, then fixed
    prompts: list[list[dict[str, str]]] = []

    def parse(text: str) -> dict[str, Any]:
        data = json.loads(text)
        assert isinstance(data, dict)
        return data

    @validated(parse, max_repairs=2)
    def ask(messages: list[dict[str, str]]) -> str:
        prompts.append(messages)
        return next(replies)

    assert ask(messages=[{"role": "user", "content": "Where?"}]) == {"city": "Oslo"}
    assert len(prompts) == 2 and prompts[1][-1]["role"] == "user"  # only the error was added


def cassette_round_trip(folder: str) -> None:
    network = FakeNetwork()
    path = os.path.join(folder, "chat.json")
    live = Cassette(path).transport(network).handle_request(Request("hello"))
    assert live.read().startswith(b"{")
    replay = Cassette(path, mode="replay")
    again = replay.transport().handle_request(Request("hello"))
    assert again.read() == live.read() and network.calls == 1
    assert "sk-example" not in Path(path).read_text()  # secrets never reach the file

    faults = LLMHTTPFaults(FaultInjector())
    flaky = faults.transport(Cassette(path, mode="replay").transport())
    with faults.rate_limit(times=1, retry_after=2):
        assert flaky.handle_request(Request("hello")).status_code == 429
    assert flaky.handle_request(Request("hello")).status_code == 200


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        shared_limits(folder)
        repaired_output()
        cassette_round_trip(folder)
    print("shared limits, validated repairs and cassettes all behaved")


if __name__ == "__main__":
    main()
