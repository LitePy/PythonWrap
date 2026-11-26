"""python-wrap: everyday Python decorators that just work.

Every decorator works on plain functions, ``async def`` functions and methods, keeps
the wrapped signature for type checkers, and never configures logging for you.

Testing helpers live in ``python_wrap.testing`` and the event catalogue in
``python_wrap.events``; they are not re-exported here.
"""

import logging
from contextlib import AbstractContextManager

from .adaptive import (
    AdaptiveLimiter,
    LoadShed,
    PerKeyLimiter,
    per_key,
)
from .aio import (
    RaceFailed,
    amap_iter,
    gather_limit,
    map_async,
    race,
    run_sync,
)
from .alerts import (
    WebhookAlerter,
)
from .async_cache import (
    AsyncCacheStore,
    AsyncMemoryStore,
    AsyncRedisStore,
    ThreadedStore,
    amemoize,
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
    ainvalidate_tags,
    cache,
    cached_property,
    invalidate,
    invalidate_tags,
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
    CancelToken,
    Debounced,
    RateLimiter,
    SignalTimerConflictWarning,
    Timeout,
    cancel_requested,
    checkpoint,
    current_token,
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
    AsyncRedisLock,
    AsyncRedisSemaphore,
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
from .http import (
    HttpRetryPolicy,
)
from .idempotency import (
    MemoryResultStore,
    SqlResultStore,
    idempotent,
)
from .lifecycle import (
    ShutdownError,
    ShutdownManager,
    ShutdownReport,
    shutdown,
)
from .local_state import (
    SQLiteBreakerState,
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
    AsyncRedisWindow,
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
    AttemptRecord,
    RetryBudget,
    RetryBudgetStats,
    Retrying,
    RetryState,
    RetryStats,
    attempt_history,
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
    AsyncRedisBreakerState,
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

__version__ = "0.4.0b2"

# A library must never configure logging; this only silences "no handler" warnings.
logging.getLogger("python_wrap").addHandler(logging.NullHandler())

__all__ = [
    "DEFAULT_REDACT",
    "INJECTED",
    "AdaptiveLimiter",
    "AlreadyRunning",
    "ArgumentTypeError",
    "AsyncBatched",
    "AsyncBuffered",
    "AsyncCacheStore",
    "AsyncDebounced",
    "AsyncMemoryStore",
    "AsyncPeriodic",
    "AsyncRedisBreakerState",
    "AsyncRedisLock",
    "AsyncRedisSemaphore",
    "AsyncRedisStore",
    "AsyncRedisWindow",
    "Attempt",
    "AttemptRecord",
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
    "CancelToken",
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
    "HttpRetryPolicy",
    "Lease",
    "LeaseLost",
    "Limited",
    "LoadShed",
    "LocalLease",
    "Memoized",
    "MemoryCheckpoint",
    "MemoryRecord",
    "MemoryResultStore",
    "Once",
    "Outcome",
    "PerKeyLimiter",
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
    "SQLiteBreakerState",
    "ShutdownError",
    "ShutdownManager",
    "ShutdownReport",
    "SignalTimerConflictWarning",
    "SqlAppLock",
    "SqlResultStore",
    "ThreadedStore",
    "Timeout",
    "TimeoutExpired",
    "TimingRecord",
    "Transaction",
    "ValidationError",
    "WebhookAlerter",
    "__version__",
    "ainvalidate_tags",
    "amap_iter",
    "amemoize",
    "aprocess_batches",
    "attempt_history",
    "audit",
    "batch_calls",
    "benchmark",
    "benchmark_async",
    "bind",
    "buffered",
    "cache",
    "cached_property",
    "cancel_requested",
    "check_type",
    "checkpoint",
    "circuit_breaker",
    "compose",
    "configure",
    "configured",
    "count_calls",
    "current_log_context",
    "current_token",
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
    "invalidate_tags",
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
    "per_key",
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
    "shutdown",
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

# Effect ledger, call ids and MCP guards (python_wrap.agents, python_wrap.mcp_guard).
from .mcp_guard import mcp_guard  # noqa: E402

__all__ += [
    "mcp_guard",
]

# Agent tool-use safety (python_wrap.agents).
__all__ += []

# OpenAI / Anthropic SDK adapters (python_wrap.adapters); no SDK is imported.
__all__ += []


def disabled() -> AbstractContextManager[None]:
    """Temporarily bypass caches, retry attempts and rate limits; see testing.disabled."""
    from .testing import disabled as scope

    return scope()


# LLM, agent and MCP helpers live in their submodules. The old top-level names keep
# working (with a DeprecationWarning) until 0.6.0.
_MOVED: dict[str, str] = {
    "AgentError": "python_wrap.agents",
    "AgentRun": "python_wrap.agents",
    "ApprovalDenied": "python_wrap.agents",
    "ApprovalTimeout": "python_wrap.agents",
    "AsyncRedisTokenWindow": "python_wrap.llm_shared",
    "AuditTape": "python_wrap.genai",
    "Budget": "python_wrap.llm",
    "BudgetBackend": "python_wrap.llm",
    "BudgetExceeded": "python_wrap.llm",
    "CacheGuard": "python_wrap.adapters._sdk",
    "Cassette": "python_wrap.llm_cassette",
    "CassetteError": "python_wrap.llm_cassette",
    "CassetteMiss": "python_wrap.llm_cassette",
    "DryRun": "python_wrap.agents",
    "EffectConflict": "python_wrap.agents",
    "EffectLedger": "python_wrap.agents",
    "EffectRecord": "python_wrap.agents",
    "EffectResolved": "python_wrap.agents",
    "EffectStore": "python_wrap.agents",
    "EffectUnknown": "python_wrap.agents",
    "FakeProvider": "python_wrap.llm_testing",
    "FakeResponse": "python_wrap.llm_testing",
    "FakeStream": "python_wrap.llm_testing",
    "LLMFaults": "python_wrap.llm_testing",
    "LoopDetected": "python_wrap.agents",
    "MCPGuard": "python_wrap.mcp_guard",
    "MemoryEffectStore": "python_wrap.agents",
    "RecordingTransport": "python_wrap.llm_cassette",
    "Redactor": "python_wrap.genai",
    "RedisBudgetBackend": "python_wrap.llm",
    "RedisEffectStore": "python_wrap.agents",
    "RedisTokenWindow": "python_wrap.llm_shared",
    "ReplayMismatch": "python_wrap.genai",
    "ReplayProvider": "python_wrap.genai",
    "ReplayTransport": "python_wrap.llm_cassette",
    "ReplayedError": "python_wrap.genai",
    "ResumableStream": "python_wrap.adapters._sdk",
    "RunBudgetExceeded": "python_wrap.agents",
    "RunCancelled": "python_wrap.agents",
    "RunContext": "python_wrap.genai",
    "RunGuard": "python_wrap.agents",
    "RunTimeout": "python_wrap.agents",
    "SQLiteBudgetBackend": "python_wrap.llm_shared",
    "SQLiteEffectStore": "python_wrap.agents",
    "SQLiteTokenWindow": "python_wrap.llm_shared",
    "SharedTokenWindow": "python_wrap.llm_shared",
    "Spend": "python_wrap.llm",
    "Step": "python_wrap.agents",
    "StepLimitExceeded": "python_wrap.agents",
    "StreamInterrupted": "python_wrap.adapters._sdk",
    "StreamTimeout": "python_wrap.llm",
    "TapeRecord": "python_wrap.genai",
    "TokenRateLimiter": "python_wrap.llm",
    "ToolCall": "python_wrap.agents",
    "ToolNotAllowed": "python_wrap.agents",
    "ToolPolicy": "python_wrap.agents",
    "ToolRegistry": "python_wrap.agents",
    "ToolSpec": "python_wrap.agents",
    "Usage": "python_wrap.llm",
    "ValidationRepairExhausted": "python_wrap.llm_validate",
    "agent_run": "python_wrap.agents",
    "budget": "python_wrap.llm",
    "current_run": "python_wrap.agents",
    "estimate_tokens": "python_wrap.llm",
    "extract_usage": "python_wrap.genai",
    "fake_llm": "python_wrap.llm_testing",
    "fallback_models": "python_wrap.llm",
    "guard_anthropic": "python_wrap.adapters.anthropic",
    "guard_openai": "python_wrap.adapters.openai",
    "instrument": "python_wrap.genai",
    "llm_cache": "python_wrap.llm",
    "llm_retry": "python_wrap.llm",
    "llm_retry_after": "python_wrap.llm",
    "llm_transient": "python_wrap.llm",
    "mcp_breaker": "python_wrap.mcp_guard",
    "no_sdk_retries": "python_wrap.adapters._sdk",
    "parse_rate_limit_headers": "python_wrap.llm_shared",
    "read_usage": "python_wrap.llm",
    "record_step": "python_wrap.agents",
    "replay": "python_wrap.genai",
    "resume_safe": "python_wrap.adapters._sdk",
    "run_context": "python_wrap.genai",
    "stream_timeout": "python_wrap.llm",
    "token_rate_limit": "python_wrap.llm",
    "tool": "python_wrap.agents",
    "tool_call_id": "python_wrap.agents",
    "tool_spec": "python_wrap.agents",
    "traced_llm": "python_wrap.genai",
    "traced_tool": "python_wrap.genai",
    "validated": "python_wrap.llm_validate",
    "with_call_id": "python_wrap.agents",
}


def __getattr__(name: str) -> object:
    module = _MOVED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    import warnings

    warnings.warn(
        f"python_wrap.{name} moved: import it from {module} (the top-level name is "
        "removed in 0.6.0)",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_MOVED})
