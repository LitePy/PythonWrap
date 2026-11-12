# Building with AI coding agents

## The problem

An AI coding agent that adds retries, caching or rate limits to your code tends to guess: unbounded retries, retried POSTs, decorators in the wrong order. python-wrap ships what an agent needs to get it right without guessing, all offline and read-only.

## The workflow

1. **Give the agent the index.** Point it at `llms.txt` (a short map) or `llms-full.txt` (everything) from the docs site, or have it run the same material locally:

    ```
    python -m python_wrap guide          # the rules: layer order, idempotency, what never to retry
    python -m python_wrap api            # every public name with a one-line summary
    python -m python_wrap api retry      # signature, parameters and docs for one name
    python -m python_wrap recipe "retry flaky llm calls"   # need -> APIs + a runnable snippet
    ```

2. **Register the MCP server** so the agent can call the same tools natively: `python -m python_wrap mcp` (add `--allow myapp` to limit what `doctor` may import). Tools: `search_api`, `get_symbol`, `recipe`, `doctor`, `explain_policy`, `migrate_preview`. The server is read-only and never calls your code.

3. **Let the agent write the code**, using a recipe or the preset closest to the need (`presets.http_client`, `presets.llm_client`).

4. **Close the loop with the doctor.** Have the agent run it on what it wrote and fix every finding before it finishes:

    ```
    python -m python_wrap doctor myapp.clients:users_policy
    python -m python_wrap doctor --multiprocess myapp.clients:users_policy
    ```

    The doctor checks stacking order, unbounded retries, retried non-idempotent calls, missing timeouts and budgets, and unbounded agent runs (codes PW001 to PW019). Exit status is non-zero on errors, so it also works in CI.

5. **Migrate old code.** `python -m python_wrap migrate src/` previews the rewrite of tenacity/backoff decorators and hand-rolled LLM retry loops; the agent applies `--write` only after you review the diff.

## A check an agent can run

```python
from python_wrap.doctor import explain
from python_wrap.introspect import find_recipes, get_symbol
from python_wrap.presets import http_client

recipe = find_recipes("retry flaky http calls")[0]
assert get_symbol("retry") is not None and recipe.snippet

users_api = http_client("users", timeout_seconds=3)

@users_api
def get_user(user_id: int) -> dict[str, int]:
    return {"id": user_id}

report = explain(get_user)
assert report.ok, [finding.code for finding in report.findings]
```

## Pitfalls

- Tell the agent which calls are idempotent; retries of writes without an idempotency key are the most expensive mistake (PW002).
- Do not let an agent set `--allow` to everything on a machine with secrets in the environment: importing a module runs its top-level code.
- Prefer recipes over free-form code, and treat a doctor finding as a failing test.

See [Agent-friendly tooling](../features/ai/agent-friendly.md) for the full reference of the commands and the MCP tools.
