"""Adapters for provider SDKs and agent frameworks.

Each adapter is a plain python-wrap module that reads its SDK by shape: none of them
imports the SDK, so ``python-wrap`` keeps zero runtime dependencies and importing
``python_wrap.adapters`` costs nothing until a name is used (exports load lazily).

- `guard_openai` / `guard_anthropic`: proxies around the OpenAI and Anthropic Python
  SDK clients with one retry loop, a `Budget`, a token limit fed by the provider's
  rate-limit headers, and resume-safe streams.
- `no_sdk_retries`, `sdk_max_retries`: switch the SDK's own retries off.
- `resume_safe`, `ResumableStream`, `StreamInterrupted`, `StreamedToolCall`: reopen
  a stream only while no output has been handed out.
- `CacheGuard`, `CacheStats`, `PromptCacheWarning`: watch prompt-cache hit rates.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from python_wrap.adapters._sdk import (
        CacheGuard,
        CacheStats,
        GuardedClient,
        PromptCacheWarning,
        ResumableStream,
        StreamedToolCall,
        StreamInterrupted,
        no_sdk_retries,
        resume_safe,
        sdk_max_retries,
        seed_limiter,
        stream_transient,
    )
    from python_wrap.adapters.anthropic import GuardedAnthropic, guard_anthropic
    from python_wrap.adapters.openai import GuardedOpenAI, guard_openai

#: Public name -> submodule that defines it (imported on first access).
_EXPORTS: dict[str, str] = {
    "CacheGuard": "_sdk",
    "CacheStats": "_sdk",
    "GuardedClient": "_sdk",
    "PromptCacheWarning": "_sdk",
    "ResumableStream": "_sdk",
    "StreamInterrupted": "_sdk",
    "StreamedToolCall": "_sdk",
    "no_sdk_retries": "_sdk",
    "resume_safe": "_sdk",
    "sdk_max_retries": "_sdk",
    "seed_limiter": "_sdk",
    "stream_transient": "_sdk",
    "GuardedOpenAI": "openai",
    "guard_openai": "openai",
    "GuardedAnthropic": "anthropic",
    "guard_anthropic": "anthropic",
}

__all__ = [
    "CacheGuard",
    "CacheStats",
    "GuardedAnthropic",
    "GuardedClient",
    "GuardedOpenAI",
    "PromptCacheWarning",
    "ResumableStream",
    "StreamInterrupted",
    "StreamedToolCall",
    "guard_anthropic",
    "guard_openai",
    "no_sdk_retries",
    "resume_safe",
    "sdk_max_retries",
    "seed_limiter",
    "stream_transient",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
