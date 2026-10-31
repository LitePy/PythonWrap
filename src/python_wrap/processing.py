"""Per-item batch outcomes and explicit checkpoints for resumable processing."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, overload

from python_wrap import _core
from python_wrap._runtime import offload_settled

__all__ = [
    "BatchResult",
    "MemoryCheckpoint",
    "Outcome",
    "RedisCheckpoint",
    "aprocess_batches",
    "process_batches",
]
T = TypeVar("T")
R = TypeVar("R")
_STATUSES = {"failed", "parked", "skipped", "conflict"}


@dataclass(frozen=True)
class Outcome(Generic[T, R]):
    """One input's result, classification and original exception when it failed."""

    index: int
    item: T
    status: str
    result: R | None = None
    exception: Exception | None = None


@dataclass
class BatchResult(Generic[T, R]):
    """Collected outcomes, preserving input order. O(number of processed items) storage."""

    outcomes: list[Outcome[T, R]] = field(default_factory=list)

    @property
    def succeeded(self) -> list[Outcome[T, R]]:
        return [item for item in self.outcomes if item.status == "succeeded"]

    @property
    def failed(self) -> list[Outcome[T, R]]:
        return [item for item in self.outcomes if item.status != "succeeded"]

    def to_rows(self, *, reason_column: str = "reason") -> list[dict[str, Any]]:
        """Convert failed items into rows with an error reason and classification."""
        return [
            {
                **(dict(o.item) if isinstance(o.item, Mapping) else {"item": o.item}),
                "status": o.status,
                reason_column: str(o.exception),
            }
            for o in self.failed
        ]


class MemoryCheckpoint:
    """A local next-index checkpoint; use one instance per input stream."""

    def __init__(self) -> None:
        self.index = 0

    def load(self) -> int:
        return self.index

    def save(self, index: int) -> None:
        self.index = index


class RedisCheckpoint:
    """Persistent next-index checkpoint; input ordering must remain stable on restart."""

    def __init__(self, client: Any, key: str) -> None:
        self.client, self.key = client, key

    def load(self) -> int:
        return int(self.client.get(self.key) or 0)

    def save(self, index: int) -> None:
        self.client.set(self.key, index)


def _failed(
    index: int, item: Any, exc: Exception, classify: Callable[[Exception], str]
) -> Outcome[Any, Any]:
    status = classify(exc)
    if status not in _STATUSES:
        raise ValueError(f"unknown batch classification: {status!r}")
    return Outcome(index, item, status, exception=exc)


def process_batches(
    items: Iterable[T],
    handler: Callable[[T], R],
    *,
    chunk_size: int = 500,
    classify: Callable[[Exception], str] = lambda exc: "failed",
    checkpoint: Any = None,
    on_progress: Callable[[int, int], Any] | None = None,
) -> BatchResult[T, R]:
    """Process items independently, persisting the next index after each chunk.

    Failed items are recorded and count as processed. A crash before a chunk checkpoint
    can replay that chunk: handlers must be idempotent when side effects matter. One
    caller must own the checkpoint (use single_instance for distributed exclusion).
    on_progress receives (processed_this_call, next_input_index).
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    if _core.is_async(handler):
        raise TypeError("use aprocess_batches for an async handler")
    result: BatchResult[T, R] = BatchResult()
    start = checkpoint.load() if checkpoint is not None else 0
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("checkpoint must contain a nonnegative next index")
    next_index = start
    for index, item in enumerate(items):
        if index < start:
            continue
        try:
            value = handler(item)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError("synchronous batch handler returned an awaitable")
        except Exception as exc:
            outcome = _failed(index, item, exc, classify)
        else:
            outcome = Outcome(index, item, "succeeded", value)
        result.outcomes.append(outcome)
        next_index = index + 1
        if len(result.outcomes) % chunk_size == 0:
            if checkpoint is not None:
                checkpoint.save(next_index)
            if on_progress:
                on_progress(len(result.outcomes), next_index)
    if result.outcomes and len(result.outcomes) % chunk_size:
        if checkpoint is not None:
            checkpoint.save(next_index)
        if on_progress:
            on_progress(len(result.outcomes), next_index)
    return result


@overload
async def aprocess_batches(
    items: Iterable[T],
    handler: Callable[[T], Awaitable[R]],
    *,
    chunk_size: int = 500,
    classify: Callable[[Exception], str] = ...,
    checkpoint: Any = None,
    on_progress: Callable[[int, int], Any] | None = None,
) -> BatchResult[T, R]: ...
@overload
async def aprocess_batches(
    items: Iterable[T],
    handler: Callable[[T], R],
    *,
    chunk_size: int = 500,
    classify: Callable[[Exception], str] = ...,
    checkpoint: Any = None,
    on_progress: Callable[[int, int], Any] | None = None,
) -> BatchResult[T, R]: ...
async def aprocess_batches(
    items: Iterable[Any],
    handler: Callable[[Any], Any],
    *,
    chunk_size: int = 500,
    classify: Callable[[Exception], str] = lambda exc: "failed",
    checkpoint: Any = None,
    on_progress: Callable[[int, int], Any] | None = None,
) -> BatchResult[Any, Any]:
    """Await per-item processing with the same checkpoint and outcome semantics.

    Synchronous handlers and checkpoint I/O run in threads and are settled before
    cancellation exits, so an enclosing lease is not released while they still write.
    Configure client-level I/O timeouts for synchronous work. Cancellation is not classified as an
    item failure, and the uncheckpointed chunk may be replayed on restart.
    """
    import asyncio

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    start = await asyncio.to_thread(checkpoint.load) if checkpoint is not None else 0
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("checkpoint must contain a nonnegative next index")
    result: BatchResult[Any, Any] = BatchResult()

    async def progress(next_index: int) -> None:
        if checkpoint is not None:
            await offload_settled(checkpoint.save, next_index)
        if on_progress:
            if _core.is_async(on_progress):
                value = on_progress(len(result.outcomes), next_index)
            else:
                value = await asyncio.to_thread(on_progress, len(result.outcomes), next_index)
            if inspect.isawaitable(value):
                await value

    next_index = start
    for index, item in enumerate(items):
        if index < start:
            continue
        try:
            value = (
                handler(item) if _core.is_async(handler) else await offload_settled(handler, item)
            )
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            outcome = _failed(index, item, exc, classify)
        else:
            outcome = Outcome(index, item, "succeeded", value)
        result.outcomes.append(outcome)
        next_index = index + 1
        if len(result.outcomes) % chunk_size == 0:
            await progress(next_index)
    if result.outcomes and len(result.outcomes) % chunk_size:
        await progress(next_index)
    return result
