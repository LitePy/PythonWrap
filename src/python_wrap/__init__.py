"""python-wrap: everyday Python decorators that just work.

Every decorator works on plain functions, ``async def`` functions and methods, keeps
the wrapped signature for type checkers, and never configures logging for you.
"""

import logging

from .caching import CacheInfo, cache, memoize, once
from .circuit import CircuitBreaker, CircuitState, circuit_breaker
from .concurrency import (
    AsyncDebounced,
    Debounced,
    debounce,
    rate_limit,
    run_in_thread,
    singleflight,
    throttle,
    timeout,
)
from .devtools import compose, deprecate, mock_data, no_debug
from .errors import (
    ArgumentTypeError,
    CircuitOpenError,
    PythonWrapError,
    RateLimitExceeded,
    TimeoutExpired,
    ValidationError,
)
from .logs import DEFAULT_REDACT, AuditRecord, audit, log_args, log_return, trace
from .retry import RetryState, fallback, retry, retry_on_exception, suppress
from .state import Transaction, current_transaction, revert_on_failure, transactional
from .timing import (
    BenchmarkResult,
    MemoryRecord,
    TimingRecord,
    benchmark,
    benchmark_async,
    measure_memory,
    profile,
    timing,
)
from .validation import check_type, enforce_types, validate_args

__version__ = "0.2.0"

# A library must never configure logging; this only silences "no handler" warnings.
logging.getLogger("python_wrap").addHandler(logging.NullHandler())

__all__ = [
    "DEFAULT_REDACT",
    "ArgumentTypeError",
    "AsyncDebounced",
    "AuditRecord",
    "BenchmarkResult",
    "CacheInfo",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "Debounced",
    "MemoryRecord",
    "PythonWrapError",
    "RateLimitExceeded",
    "RetryState",
    "TimeoutExpired",
    "TimingRecord",
    "Transaction",
    "ValidationError",
    "__version__",
    "audit",
    "benchmark",
    "benchmark_async",
    "cache",
    "check_type",
    "circuit_breaker",
    "compose",
    "current_transaction",
    "debounce",
    "deprecate",
    "enforce_types",
    "fallback",
    "log_args",
    "log_return",
    "measure_memory",
    "memoize",
    "mock_data",
    "no_debug",
    "once",
    "profile",
    "rate_limit",
    "retry",
    "retry_on_exception",
    "revert_on_failure",
    "run_in_thread",
    "singleflight",
    "suppress",
    "throttle",
    "timeout",
    "timing",
    "trace",
    "transactional",
    "validate_args",
]
