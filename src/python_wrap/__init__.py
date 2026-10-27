"""python-wrap: everyday Python decorators that just work."""

import logging

from .errors import (
    ArgumentTypeError,
    CircuitOpenError,
    PythonWrapError,
    RateLimitExceeded,
    TimeoutExpired,
    ValidationError,
)

__version__ = "0.2.0.dev0"

# A library must never configure logging; this only silences "no handler" warnings.
logging.getLogger("python_wrap").addHandler(logging.NullHandler())

__all__ = [
    "ArgumentTypeError",
    "CircuitOpenError",
    "PythonWrapError",
    "RateLimitExceeded",
    "TimeoutExpired",
    "ValidationError",
    "__version__",
]
