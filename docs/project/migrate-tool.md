# The migration tool

`python -m python_wrap migrate` rewrites the retry decorators of **tenacity** and **backoff** to `python_wrap.retry`, and hand-rolled retry loops around LLM client calls to `llm_retry` (see [LLM retry loops](#llm-retry-loops)). It is a small, conservative codemod built on the standard library (`ast` plus text slicing, so formatting and comments outside the decorator are untouched). It never imports tenacity or backoff and needs neither installed.

```
python -m python_wrap migrate [--write] [--diff] PATH...
```

| Option | Effect |
| --- | --- |
| (none) | dry run: report what would change and what needs manual work |
| `--diff` | also print a unified diff of every file that would change |
| `--write` | write the rewritten files |
| `PATH` | files or directories; directories are searched recursively for `*.py`, skipping `.venv`, `venv`, `.git`, `__pycache__`, `.tox`, `.mypy_cache` and `node_modules` |

**Exit status.** `0` when nothing needs manual attention, `1` when at least one unsupported construct was found (also for a file that does not parse), `2` for an unreadable path. In CI, run it without `--write` to check that no tenacity or backoff decorators remain.

A typical session:

```
$ python -m python_wrap migrate --diff src/
src/app/client.py:12:2: rewrote decorator
src/app/client.py:30:2: unsupported: before_sleep= hooks receive tenacity's RetryState; ...
$ python -m python_wrap migrate --write src/
```

Review the diff. Notes printed under each rewrite list behaviour differences specific to that decorator (see below).

## From Python

The same logic is available as a pure function:

```python
from python_wrap.migrate import migrate_source

source = """\
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, max=30))
def fetch(): ...
"""
result = migrate_source(source)
print(result.text)
assert "@retry(max_attempts=5, delay=1, backoff=2, max_delay=30)" in result.text
assert result.unsupported == []
assert result.changes[0].line == 3
```

`MigrationResult` has `text` (the new source), `changes` (a `Change` per rewritten decorator, with `before`, `after` and `notes`) and `unsupported` (`Unsupported(line, col, message)` findings). Running the tool again on its own output changes nothing.

## What it rewrites

### tenacity `@retry(...)` and `@tenacity.retry(...)`

| tenacity | becomes |
| --- | --- |
| `stop_after_attempt(n)` | `max_attempts=n` |
| `stop_after_delay(s)` | `max_time=s` |
| `stop_after_attempt(n) \| stop_after_delay(s)` | both |
| `stop_never`, or no `stop=` | `max_attempts=None` |
| `wait_fixed(s)` | `delay=s, backoff=1` |
| `wait_none()`, or no `wait=` | `delay=0, backoff=1` |
| `wait_exponential(multiplier=m, min=a, max=b, exp_base=e)` | `delay=max(m, a), backoff=e, max_delay=b` (approximation when `min` is nonzero) |
| `wait_random(a, b)` | `delay=a, backoff=1, jitter=b - a` |
| `wait_fixed(s) + wait_random(a, b)` | `delay=s + a, backoff=1, jitter=b - a` |
| `wait_exponential_jitter(initial, max, exp_base, jitter)` | `delay=initial, backoff=exp_base, max_delay=max, jitter=jitter` |
| `retry_if_exception_type(X)` | `on=X` |
| `retry_if_exception(pred)` | `retry_if=pred` |
| `retry_if_result(pred)` | `retry_if_result=pred` and `on=()` |
| `retry_if_exception_type(X) \| retry_if_result(pred)` | `on=X, retry_if_result=pred` |
| `retry_if_exception_type(X) & retry_if_exception(pred)` | `on=X, retry_if=pred` |
| `reraise=True` | dropped (python-wrap always re-raises the original) |
| bare `@retry` | `@retry(max_attempts=None, delay=0, backoff=1)` (tenacity's bare form retries forever, and so does this) |

### backoff `@backoff.on_exception(...)` and `@backoff.on_predicate(...)`

| backoff | becomes |
| --- | --- |
| `backoff.expo` with `factor`, `base`, `max_value` | `delay=factor, backoff=base, max_delay=max_value` (defaults 1 and 2) |
| `backoff.constant` with `interval` | `delay=interval, backoff=1` |
| `exception` (2nd argument) | `on=` |
| `max_tries`, `max_time` | `max_attempts`, `max_time` (omitted `max_tries` means unlimited: `max_attempts=None`) |
| `jitter=None` | no jitter |
| default `jitter=backoff.full_jitter` or explicit `full_jitter` | no jitter is added, and a note is printed |
| `logger=` | `logger=` |
| `on_predicate(predicate=p)` | `on=(), retry_if_result=p` (default predicate: `lambda result: not result`) |

### Imports

Imports from `tenacity` and `backoff` that are no longer used are removed, and `from python_wrap import retry` is added where the first one was. Names that are still referenced, such as `Retrying` or `RetryError`, stay and are reported. If a still-used tenacity `retry` (or any other `retry` in the module) would clash with the new name, the new one is imported as `python_wrap_retry`.

## LLM retry loops

Many services retry LLM calls with a `for`/`while` loop, a `try`/`except` and a `time.sleep`. The codemod finds these by the shape of the call, with no SDK import needed: a loop containing a `try` and a `sleep(...)`/`time.sleep(...)`/`asyncio.sleep(...)` around a call whose attribute path ends in `.messages.create`, `.chat.completions.create`, `.completions.create`, `.responses.create`, `.generate_content` or `.generate_content_async`.

A loop is **rewritten** only when every one of these holds (otherwise nothing is touched):

- it is `for <name> in range(<integer literal >= 2>)` with no `else`, and is the whole body of a function (a docstring is fine), so nothing else would be retried or lost;
- the loop body is one `try` whose body is exactly `return <llm call>` (or `return await <llm call>`), with no `else`/`finally`, and the call does not use the loop variable;
- each `except` block only sleeps (plus `pass`, `continue` or the loop's own `if attempt == N - 1: raise`) and does not use the caught exception;
- there is exactly one sleep, whose argument is a number or `<number> * 2 ** <attempt>`.

The function then gets `@llm_retry(max_attempts=N, delay=<first sleep>)` as its innermost decorator, the loop becomes `return <call>`, and `from python_wrap.llm import llm_retry` is added after the last top-level import (an existing `llm_retry` import is reused). Notes on the change list the behaviour differences: only transient errors are retried, waits double with jitter and honour `Retry-After`, and the last error is raised when attempts run out. `time`/`asyncio` imports that become unused are left for your linter.

Every other retry loop (a `while` loop, logging in the `except`, code or a final `raise` after the loop, a computed sleep, state kept between attempts) is **reported, not rewritten**, with the line range, the call, the reason and the fix:

```
svc.py:5:5: unsupported: hand-rolled retry loop around `client.responses.create(...)` (lines 5-10) not rewritten: an except block does more than sleep (logging, state changes or other statements; line 8). Move the single call into its own function decorated with `@llm_retry(max_attempts=..., delay=...)` (from python_wrap.llm) and delete the loop, the sleep and the except block
```

Dry run, `--diff`, `--write`, exit status and the parse check work exactly as for decorators; the MCP `migrate_preview` tool runs the same code. A file with both tenacity decorators and retry loops is handled in one pass, and reported line numbers refer to the original file.

```python
from python_wrap.migrate import migrate_source

source = """\
import time


def ask(client, prompt):
    for attempt in range(3):
        try:
            return client.messages.create(model="m-1", messages=prompt)
        except Exception:
            time.sleep(1)
"""
result = migrate_source(source)
assert "@llm_retry(max_attempts=3, delay=1)" in result.text
assert result.changes[0].kind == "loop" and result.changes[0].line == 5
```

## What it reports and leaves alone

Each finding is printed as `path:line:col: unsupported: message` and the code is left exactly as it was:

- `before_sleep=`, `before=`, `after=` hooks (the callback signature changes; port them to `on_retry=`, see [the tenacity guide](migrating-from-tenacity.md#hooks))
- `retry_error_callback=`, `retry_error_cls=`, `sleep=`, `statistics=`
- `wait_chain`, `wait_incrementing`, `wait_random_exponential`, combined or custom strategies, `stop_any`/`stop_all`, variables used in place of strategy objects
- `retry_if_not_*`, `retry_if_exception_message`, other retry combinators
- `backoff.fibo`, custom `wait_gen`, `giveup=`, `on_backoff=`, `on_success=`, `on_giveup=`, `raise_on_giveup=False`, custom `jitter` functions, callable `max_tries`/`max_time`
- `Retrying(...)`, `AsyncRetrying(...)`, `retry_object`, `.retry.statistics`, `.retry_with(...)` and any other use of a tenacity or backoff name outside a decorator
- `from tenacity import *`
- files that do not parse

If a file would end up with invalid syntax after the rewrite, the tool aborts that file's change and reports it; it never writes code that does not parse (`ast.parse` is checked before anything is written).

## Behaviour differences the tool cannot fix

The rewrite keeps the *configuration*; some semantics still change. Each rewritten decorator carries notes for the ones that apply to it:

- tenacity raised `RetryError` unless `reraise=True`; python-wrap re-raises the original exception. Update `except RetryError` handlers.
- With `retry_if_result`, python-wrap returns the last rejected value when attempts run out; tenacity raised.
- `stop_after_delay` becomes `max_time`, which refuses a wait that would overrun the budget.
- `wait_exponential(min=...)` and backoff's `full_jitter` are approximated, as noted.
- `asyncio.CancelledError`, `KeyboardInterrupt` and `SystemExit` are never retried.

See the full guides: [tenacity](migrating-from-tenacity.md), [pybreaker](migrating-from-pybreaker.md) and [limits](migrating-from-limits.md) (these two are manual). For the python-wrap side of the story, read [retry](../features/resilience/retry.md).
