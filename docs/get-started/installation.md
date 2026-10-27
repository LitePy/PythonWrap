# Installation

python-wrap needs Python 3.10 or newer and has no runtime dependencies. It runs on Linux, macOS and Windows.

## Install

=== "pip"

    ```bash
    pip install python-wrap
    ```

=== "uv"

    ```bash
    uv add python-wrap
    ```

=== "poetry"

    ```bash
    poetry add python-wrap
    ```

The package name on PyPI is `python-wrap`; the import name uses an underscore, `python_wrap`.

!!! note "Zero dependencies"
    `dependencies = []` is deliberate. Adding python-wrap to an application, or to a library you publish, pulls in nothing else. The testing helpers in `python_wrap.testing` use only the standard library too; pytest is optional.

## Verify the install

Run this in the Python you installed into. It uses a decorator, so it checks the import and that the package works:

```python
import python_wrap
from python_wrap import retry

attempts = []

@retry(max_attempts=3, delay=0)
def flaky() -> str:
    attempts.append(1)
    if len(attempts) < 3:
        raise ConnectionError("try again")
    return "ok"

assert flaky() == "ok"
print("python-wrap", python_wrap.__version__, "works")
```

From a shell, `pip show python-wrap` (or `uv pip show python-wrap`) prints the installed version.

## Type checkers

python-wrap ships a `py.typed` marker and is checked with `mypy --strict`. Decorators preserve the wrapped function's signature with `ParamSpec`, so mypy, pyright and your editor still know what a decorated function takes and returns. There is nothing to configure.

```python
from python_wrap import memoize

@memoize(ttl=60)
def square(n: int) -> int:
    return n * n

result = square(4)       # a type checker infers int
assert result == 16
assert square.cache_info().hits == 0     # and knows about cache_info()
```

Decorators that add attributes to the function (for example `cache_info()`, `breaker`, `stats`) return a typed wrapper that declares them, so `square.cache_info()` type-checks.

## Logging

Importing python-wrap never configures logging. Decorators log to the `python_wrap` logger, which has a `NullHandler`, so you see nothing until you opt in. See [Logging](../features/observability/logging.md).

## Upgrading from 0.1

Version 0.2 renames and restructures some APIs. The [migration guide](../project/migration.md) lists every change with before and after code.

## Next

Build something with it in the [Quickstart](quickstart.md), or find the decorator for your problem in [Choosing a decorator](choosing.md).
