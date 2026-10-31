"""python-wrap: everyday Python decorators that just work.

Every decorator works on plain functions, ``async def`` functions and methods, keeps
the wrapped signature for type checkers, and never configures logging for you.

Testing helpers live in ``python_wrap.testing`` and the event catalogue in
``python_wrap.events``; they are not re-exported here.
"""

import logging
from contextlib import AbstractContextManager

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
from .buffering import (
    AsyncBuffered,
    Buffered,
    BufferStats,
    buffered,
)
from .bulkhead import (
    Bulkhead,
    Limited,
    limit_concurrency,
)
from .caching import (
    CachedProperty,
    CacheInfo,
    CacheStore,
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
from .coordination import (
    AlreadyRunning,
    Lease,
    LeaseLost,
    LocalLease,
    PgAdvisoryLock,
    RedisLock,
    RedisSemaphore,
    SqlAppLock,
    single_instance,
)
from .credentials import (
    Expiring,
    expiring,
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
from .health import (
    HealthRegistry,
    health,
)
from .hedging import (
    hedge,
)
from .idempotency import (
    MemoryResultStore,
    idempotent,
)
from .logs import (
    DEFAULT_REDACT,
    AuditRecord,
    ContextFilter,
    audit,
    bind,
    current_log_context,
    log_args,
    log_context,
    log_return,
    trace,
    unbind,
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
from .policies import (
    PolicyRegistry,
    policies,
    resilient,
)
from .policy import (
    Policy,
    policy,
)
from .predicates import (
    Predicate,
    db_transient,
    error_code,
    in_cause_chain,
    message_contains,
    mysql_transient,
    postgres_transient,
    sqlserver_transient,
    sqlstate,
)
from .processing import (
    BatchResult,
    MemoryCheckpoint,
    Outcome,
    RedisCheckpoint,
    aprocess_batches,
    process_batches,
)
from .quotas import (
    Quota,
    QuotaExhausted,
    RedisWindow,
    shared_rate_limit,
)
from .redaction import (
    is_sensitive,
    redact,
)
from .retry import (
    Attempt,
    RetryBudget,
    RetryBudgetStats,
    Retrying,
    RetryState,
    RetryStats,
    fallback,
    http_status,
    retry,
    retry_after_header,
    retry_on_exception,
    retry_stats,
    retrying,
    suppress,
)
from .shared import (
    RedisBreakerState,
)
from .slow import (
    timed,
)
from .state import (
    INJECTED,
    Transaction,
    current_transaction,
    require_transaction,
    revert_on_failure,
    transactional,
)
from .stores import (
    RedisStore,
)
from .switches import (
    EnvSource,
    FeatureDisabled,
    RedisSource,
    switch,
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

__version__ = "0.3.0"

# A library must never configure logging; this only silences "no handler" warnings.
logging.getLogger("python_wrap").addHandler(logging.NullHandler())

__all__ = [
    "DEFAULT_REDACT",
    "INJECTED",
    "AlreadyRunning",
    "ArgumentTypeError",
    "AsyncBatched",
    "AsyncBuffered",
    "AsyncDebounced",
    "AsyncPeriodic",
    "Attempt",
    "AuditRecord",
    "BatchError",
    "BatchResult",
    "Batched",
    "BenchmarkResult",
    "BufferStats",
    "Buffered",
    "Bulkhead",
    "BulkheadFull",
    "CacheInfo",
    "CacheStore",
    "CachedProperty",
    "CallStats",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitProtected",
    "CircuitState",
    "CircuitStats",
    "ContextFilter",
    "Counted",
    "Deadline",
    "Debounced",
    "EnvSource",
    "Expiring",
    "FeatureDisabled",
    "HealthRegistry",
    "Lease",
    "LeaseLost",
    "Limited",
    "LocalLease",
    "Memoized",
    "MemoryCheckpoint",
    "MemoryRecord",
    "MemoryResultStore",
    "Once",
    "Outcome",
    "Periodic",
    "PgAdvisoryLock",
    "Policy",
    "PolicyRegistry",
    "Predicate",
    "PythonWrapError",
    "Quota",
    "QuotaExhausted",
    "RaceFailed",
    "RateLimitExceeded",
    "RateLimiter",
    "RedisBreakerState",
    "RedisCheckpoint",
    "RedisLock",
    "RedisSemaphore",
    "RedisSource",
    "RedisStore",
    "RedisWindow",
    "RetryBudget",
    "RetryBudgetStats",
    "RetryState",
    "RetryStats",
    "Retrying",
    "SqlAppLock",
    "Timeout",
    "TimeoutExpired",
    "TimingRecord",
    "Transaction",
    "ValidationError",
    "__version__",
    "amap_iter",
    "aprocess_batches",
    "audit",
    "batch_calls",
    "benchmark",
    "benchmark_async",
    "bind",
    "buffered",
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
    "db_transient",
    "deadline",
    "debounce",
    "deprecate",
    "disabled",
    "enforce_types",
    "error_code",
    "expiring",
    "fallback",
    "gather_limit",
    "get_config",
    "health",
    "hedge",
    "http_status",
    "idempotent",
    "in_cause_chain",
    "invalidate",
    "is_sensitive",
    "limit_concurrency",
    "limiter_of",
    "listening",
    "log_args",
    "log_context",
    "log_return",
    "map_async",
    "measure_memory",
    "memoize",
    "message_contains",
    "mock_data",
    "mysql_transient",
    "no_debug",
    "once",
    "periodic",
    "policies",
    "policy",
    "postgres_transient",
    "process_batches",
    "profile",
    "race",
    "rate_limit",
    "redact",
    "require_transaction",
    "reset_config",
    "resilient",
    "retry",
    "retry_after_header",
    "retry_on_exception",
    "retry_stats",
    "retrying",
    "revert_on_failure",
    "run_in_thread",
    "run_sync",
    "shared_rate_limit",
    "single_instance",
    "singleflight",
    "sqlserver_transient",
    "sqlstate",
    "subscribe",
    "suppress",
    "switch",
    "throttle",
    "timed",
    "timeout",
    "timing",
    "trace",
    "transactional",
    "unbind",
    "validate_args",
    "wrap_methods",
]


def disabled() -> AbstractContextManager[None]:
    """Temporarily bypass caches, retry attempts and rate limits; see testing.disabled."""
    from .testing import disabled as scope

    return scope()
