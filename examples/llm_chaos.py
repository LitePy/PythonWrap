"""Test an agent's resilience in milliseconds: fake provider + LLM faults + fake clock.

The agent below retries rate limits and overloads with exponential backoff, and validates
that the model returned JSON. No network and no SDK: ``fake_llm`` plays the model,
``chaos.llm`` breaks it on demand and ``fake_clock`` turns minutes of backoff into
nothing.

Run: python examples/llm_chaos.py
"""

from __future__ import annotations

import json
import time

from python_wrap import fake_llm, retry
from python_wrap.chaos import chaos
from python_wrap.llm_testing import OverloadedError, RateLimitError
from python_wrap.testing import fake_clock

llm = fake_llm(['{"city": "Oslo", "temp_c": 4}'], usage=(25, 9))


class BadModelOutput(ValueError):
    pass


@retry(max_attempts=5, delay=30.0, backoff=2, on=(RateLimitError, OverloadedError, BadModelOutput))
def ask_weather(question: str) -> dict[str, object]:
    reply = llm.complete(messages=[{"role": "user", "content": question}])
    try:
        data = json.loads(reply.text)
    except json.JSONDecodeError as exc:
        raise BadModelOutput("model returned broken JSON") from exc
    assert isinstance(data, dict)
    return data


def main() -> None:
    started = time.perf_counter()
    with fake_clock() as clock:
        chaos.llm.rate_limit("llm.*", times=2, retry_after=30)
        chaos.llm.overloaded("llm.*", times=1)
        chaos.llm.malformed_json("llm.*", text='{"city": "Oslo", "temp_c": 4}', times=1)
        try:
            result = ask_weather("Weather in Oslo?")
        finally:
            chaos.clear()

    assert result == {"city": "Oslo", "temp_c": 4}
    assert llm.call_count == 5 and llm.served_count == 1
    assert clock.sleeps == [30.0, 60.0, 120.0, 240.0]
    llm.assert_called_with(messages=[{"role": "user", "content": "Weather in Oslo?"}])
    llm.assert_tokens(input_tokens=25, output_tokens=9)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    print(f"survived 4 injected faults; {clock.total_slept:.0f}s of backoff took {elapsed:.3f}s")


if __name__ == "__main__":
    main()
