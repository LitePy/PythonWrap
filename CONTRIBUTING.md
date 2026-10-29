# Contributing to python-wrap

Thank you for considering it. This page covers how to set up, how to run the checks,
and the conventions every module follows.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

The project uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/LitePy/PythonWrap
cd PythonWrap
uv sync --all-groups
```

This creates `.venv/`, installs the package in editable mode and installs the `dev` and
`docs` groups. You never need to activate the virtualenv: prefix commands with `uv run`.
There is no Makefile; the commands below are the whole workflow.

## Run the checks

Run these checks before opening or merging a pull request. CI and docs run after
changes land on `main`; pull requests and feature-branch pushes do not start them.
Release publishing remains tied to version tags.

```bash
uv run pytest                                  # tests
uv run pytest --cov --cov-report=term-missing  # with coverage
uv run pytest tests/test_retry.py -k async -x  # a subset
uv run ruff check src tests                    # lint
uv run ruff format src tests                   # format (CI runs it with --check)
uv run mypy                                    # strict type check of src/
uv run mkdocs serve                            # live docs preview
uv run mkdocs build --strict                   # what the docs CI job runs
```

To test another interpreter: `uv run --python 3.10 pytest`.

Optional, and worth installing once: `uvx pre-commit install`.

## Conventions

The authoritative version is the docstring at the top of `src/python_wrap/_core.py`.
In short:

1. **Standard library only.** No runtime dependencies. Python 3.10 and newer.
2. **Sync and async.** Every decorator works on plain functions, `async def` functions
   and methods (instance, class, static). For `async def`, return an `async def`
   wrapper so `inspect.iscoroutinefunction(wrapped)` stays true, and never block the
   event loop.
3. **No logging configuration.** Never call `logging.basicConfig` or add handlers.
   Log through `_core.get_logger(logger)`; logging decorators take
   `logger: LoggerLike = None` and `level`.
4. **A patchable clock.** Read time through `_core.monotonic()` and
   `_core.perf_counter()` and wait through `_core.sleep()` and
   `await _core.async_sleep()`, always as attributes of the module
   (`from python_wrap import _core`), never imported by name.
5. **Full typing.** `mypy --strict` clean, signatures preserved with `ParamSpec`.
   Decorators usable both bare and with options use the overload pattern:

   ```python
   @overload
   def timing(func: Callable[P, R], /) -> Callable[P, R]: ...
   @overload
   def timing(func: None = None, /, *, logger: LoggerLike = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
   def timing(func=None, /, *, logger=None):
       def decorate(f): ...
       return decorate if func is None else decorate(func)
   ```
6. **Thread safety** where state is shared: locks around caches, counters and buckets.
7. **`functools.wraps`** on every wrapper, and a Google-style docstring (Args, Returns,
   Raises, Example) on every public function; the docs site renders them.
8. **`__all__`** in every module.

## Tests

Put tests in `tests/test_<module>.py`. They should be behaviour-focused and fast, cover
sync and async paths and error paths, and test thread safety where relevant.

Use the `fake_clock` fixture from `tests/conftest.py` instead of real sleeping. It
patches every time source in `_core`, and `sleep` / `async_sleep` advance the fake clock
instead of waiting:

```python
def test_retry_waits_between_attempts(fake_clock):
    ...
    assert fake_clock.sleeps == [0.5, 1.0]
```

No real sleep over 0.05 seconds.

## Pull requests

Keep them focused, add a line under `Unreleased` in `CHANGELOG.md`, and explain the
reasoning in the description. Releases are described in [RELEASING.md](RELEASING.md).
