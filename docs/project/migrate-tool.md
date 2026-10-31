# The migration tool

`python -m python_wrap migrate` rewrites the retry decorators of **tenacity** and **backoff** to `python_wrap.retry`. It is a small, conservative codemod built on the standard library (`ast` plus text slicing, so formatting and comments outside the decorator are untouched). It never imports tenacity or backoff and needs neither installed.

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
