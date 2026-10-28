"""python-wrap: everyday Python decorators that just work.

Every decorator works on plain functions, ``async def`` functions and methods, keeps
the wrapped signature for type checkers, and never configures logging for you.

Testing helpers live in ``python_wrap.testing`` and the event catalogue in
``python_wrap.events``; they are not re-exported here.
"""

import logging

from .aio import (
    RaceFailed,
    amap_iter,
    gather_limit,
    map_async,
    race,
    run_sync,
)
from .batching import (
    AsyncBatched,
    Batched,
    batch_calls,
)
from .bulkhead import (
    Bulkhead,
    Limited,
    limit_concurrency,
)
from .caching import (
    CachedProperty,
    CacheInfo,
    Memoized,
    Once,
    cache,
    cached_property,
    invalidate,
    memoize,
    once,
)
from .circuit import (
    CircuitBreaker,
    CircuitProtected,
    CircuitState,
    CircuitStats,
    circuit_breaker,
)
from .concurrency import (
    AsyncDebounced,
    Debounced,
    RateLimiter,
    Timeout,
    debounce,
    limiter_of,
    rate_limit,
    run_in_thread,
    singleflight,
    throttle,
    timeout,
)
from .config import (
    configure,
    configured,
    get_config,
    reset_config,
)
from .deadline import (
    Deadline,
    deadline,
)
from .devtools import (
    compose,
    deprecate,
    mock_data,
    no_debug,
    wrap_methods,
)
from .errors import (
    ArgumentTypeError,
    BatchError,
    BulkheadFull,
    CircuitOpenError,
    PythonWrapError,
    RateLimitExceeded,
    TimeoutExpired,
    ValidationError,
)
from .events import (
    listening,
    subscribe,
)
from .hedging import (
    hedge,
)
from .logs import (
    DEFAULT_REDACT,
    AuditRecord,
    audit,
    current_log_context,
    log_args,
    log_context,
    log_return,
    trace,
)
from .metrics import (
    CallStats,
    Counted,
    count_calls,
)
from .periodic import (
    AsyncPeriodic,
    Periodic,
    periodic,
)
from .policy import (
    Policy,
    policy,
)
from .retry import (
    Attempt,
    RetryBudget,
    RetryBudgetStats,
    Retrying,
    RetryState,
    RetryStats,
    fallback,
    retry,
    retry_after_header,
    retry_on_exception,
    retry_stats,
    retrying,
    suppress,
)
from .state import (
    INJECTED,
    Transaction,
    current_transaction,
    require_transaction,
    revert_on_failure,
    transactional,
)
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
from .validation import (
    check_type,
    enforce_types,
    validate_args,
)

__version__ = "0.2.0"

# A library must never configure logging; this only silences "no handler" warnings.
logging.getLogger("python_wrap").addHandler(logging.NullHandler())

__all__ = [
    "DEFAULT_REDACT",
    "INJECTED",
    "ArgumentTypeError",
    "AsyncBatched",
    "AsyncDebounced",
    "AsyncPeriodic",
    "Attempt",
    "AuditRecord",
    "BatchError",
    "Batched",
    "BenchmarkResult",
    "Bulkhead",
    "BulkheadFull",
    "CacheInfo",
    "CachedProperty",
    "CallStats",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitProtected",
    "CircuitState",
    "CircuitStats",
    "Counted",
    "Deadline",
    "Debounced",
    "Limited",
    "Memoized",
    "MemoryRecord",
    "Once",
    "Periodic",
    "Policy",
    "PythonWrapError",
    "RaceFailed",
    "RateLimitExceeded",
    "RateLimiter",
    "RetryBudget",
    "RetryBudgetStats",
    "RetryState",
    "RetryStats",
    "Retrying",
    "Timeout",
    "TimeoutExpired",
    "TimingRecord",
    "Transaction",
    "ValidationError",
    "__version__",
    "amap_iter",
    "audit",
    "batch_calls",
    "benchmark",
    "benchmark_async",
    "cache",
    "cached_property",
    "check_type",
    "circuit_breaker",
    "compose",
    "configure",
    "configured",
    "count_calls",
    "current_log_context",
    "current_transaction",
    "deadline",
    "debounce",
    "deprecate",
    "enforce_types",
    "fallback",
    "gather_limit",
    "get_config",
    "hedge",
    "invalidate",
    "limit_concurrency",
    "limiter_of",
    "listening",
    "log_args",
    "log_context",
    "log_return",
    "map_async",
    "measure_memory",
    "memoize",
    "mock_data",
    "no_debug",
    "once",
    "periodic",
    "policy",
    "profile",
    "race",
    "rate_limit",
    "require_transaction",
    "reset_config",
    "retry",
    "retry_after_header",
    "retry_on_exception",
    "retry_stats",
    "retrying",
    "revert_on_failure",
    "run_in_thread",
    "run_sync",
    "singleflight",
    "subscribe",
    "suppress",
    "throttle",
    "timeout",
    "timing",
    "trace",
    "transactional",
    "validate_args",
    "wrap_methods",
]
