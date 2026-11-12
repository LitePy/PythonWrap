# Support agent

A customer-support AI agent: it reads a ticket, looks the order up, refunds it (with approval) and emails the customer. A scripted fake model plays the LLM, so it runs offline, deterministically, in well under a second, and no provider SDK is imported. The names are invented.

## Run it

```console
$ pip install python-wrap
$ python main.py          # six scenarios, printed
$ python -m pytest tests  # 11 tests: happy path, denial, limits, replay, chaos, doctor
```

## Layout

| File | Role |
| --- | --- |
| `main.py` | Demo: normal run, replay from the tape, repeat, denial, rate limit, doctor |
| `support/config.py` | `Settings`: step, repeat and token limits, refund threshold |
| `support/tools.py` | `lookup_order`, `refund`, `send_email` in a `ToolRegistry` with a guard and a policy |
| `support/provider.py` | `scripted_support_model`: a `FakeProvider` reply function that scripts the tool calls |
| `support/llm.py` | The model client: `llm_client` preset, `Budget`, `fallback_models`, `traced_llm` |
| `support/agent.py` | The loop. Takes `ask` and `dispatch` callables, so replay can swap them |
| `support/observability.py` | The `Redactor` and the JSONL `AuditTape` |
| `support/backends.py` | Fake order book, payments API and mailbox |
| `support/app.py` | Wiring |

## python-wrap features used, and why

* **`@tool` with `read_only`, `requires_approval`, `idempotent_key`** (`tools.py`): `lookup_order` is read-only; `refund` needs the approver's yes and is keyed by order id, so it happens once per order even if the model asks twice or a new run starts; `send_email` is deduplicated per run, so a repeated step does not mail twice.
* **`ToolRegistry`, `RunGuard`, `ToolPolicy`** (`tools.py`): the loop calls tools by the name the model emits; step, repeat and budget limits bound every run; refunds above the limit are denied, and the agent is told, not crashed.
* **`record_step` and `Budget(per="run")`** (`agent.py`, `llm.py`): each model call counts as a step and spends from the run's token budget, so a runaway loop stops at `RunBudgetExceeded`.
* **`presets.llm_client`** (`llm.py`): spend budget, circuit breaker, retries that honour `Retry-After`, and a per-attempt timeout, in one policy.
* **`fallback_models`** (`llm.py`): when the primary model is rate limited or overloaded, the backup model answers the same turn.
* **`traced_llm` / `traced_tool` with a `Redactor` and `AuditTape`** (`llm.py`, `tools.py`, `observability.py`): every call is recorded to a JSONL file with emails, secrets and your own patterns masked.
* **`replay`** (`tests/test_agent.py`): the saved tape drives the same loop with no model and no side effects, and fails loudly if the flow changes.
* **`chaos.llm.rate_limit` with `fake_clock`** (`tests/test_agent.py`): inject 429s with `Retry-After` and check the fallback and the retry, with no waiting.
* **`doctor` / `explain`** (`tests/test_agent.py`): lints the model client stack and the tool registry (PW013-PW017 style findings).

## Copying it

Swap `support/provider.py` for your SDK client (`ask` only needs to return something with `.tool_calls` and `.text`, or a dict with those keys; token usage is read from `usage` in Anthropic or OpenAI style) and `support/backends.py` for real systems. The loop, tools and policies do not change.
