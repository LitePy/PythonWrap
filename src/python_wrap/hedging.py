"""Hedged requests: `hedge` starts a backup attempt when the first one is slow."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from python_wrap import _core

__all__ = ["hedge"]

F = TypeVar("F", bound=Callable[..., Any])

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


def _start_thread(
    func: Callable[..., Any], args: Any, kwargs: Any
) -> concurrent.futures.Future[Any]:
    """Run one attempt in a new daemon thread (with the caller's context variables)."""
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()
    context = contextvars.copy_context()

    def target() -> None:
        try:
            result = context.run(func, *args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    threading.Thread(target=target, name=f"hedge-{_core.qualname(func)}", daemon=True).start()
    return future


def hedge(
    delay: float,
    *,
    max_extra: int = 1,
    on: ExceptionTypes = Exception,
) -> Callable[[F], F]:
    """Start a backup attempt when a call is slow, and return the first success.

    !!! warning "Only for idempotent operations"
        The function may run up to `1 + max_extra` times for one call, concurrently.
        Use it only where running the operation more than once is harmless: reads,
        lookups, idempotent writes. Never on payments, sends, or increments.

    The call starts once. If it has not finished after `delay` seconds, a second attempt
    starts alongside it, and so on, up to `max_extra` extra attempts (each `delay` after
    the previous one). The first attempt to **succeed** wins. If an attempt fails with an
    exception matching `on` while others are still running, the next attempt starts
    immediately instead of waiting out the delay. An exception not matching `on` is
    raised at once. If every attempt fails, the last exception is raised.

    * `async def` functions: each attempt is a task. As soon as there is a winner (or a
      non-matching error), the other attempts are cancelled and awaited, so the wrapper
      never leaves tasks behind. Cancelling the caller cancels every attempt.
    * Sync functions: each attempt runs in its own daemon thread and the caller blocks
      until one succeeds. **Threads cannot be cancelled**: losing attempts run to
      completion in the background and their results are discarded. Their side effects
      still happen, and they keep the thread (and whatever it holds) busy until done.

    Context variables are copied into each attempt.

    Args:
        delay: Seconds to wait before starting each extra attempt (>= 0).
        max_extra: Maximum number of extra attempts (>= 0; 0 disables hedging).
        on: Exception type(s) that count as a failed attempt and trigger the next hedge.
            Anything else propagates at once.

    Returns:
        A decorator that keeps the function's signature.

    Raises:
        ValueError: If `delay` < 0 or `max_extra` < 0.

    Example:
        >>> @hedge(0.05, max_extra=2)
        ... async def get_price(sku: str) -> float:
        ...     ...  # a read from a service with an occasional slow replica
    """
    if delay < 0:
        raise ValueError(f"hedge delay must be non-negative, got {delay!r}")
    if type(max_extra) is not int or max_extra < 0:
        raise ValueError(f"hedge max_extra must be an integer >= 0, got {max_extra!r}")
    retry_on = on
    max_attempts = 1 + max_extra

    def decorate(func: F) -> F:
        if _core.is_async(func):
            async_func = cast(Callable[..., Awaitable[Any]], func)

            async def attempt(args: Any, kwargs: Any) -> Any:
                return await async_func(*args, **kwargs)

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                loop = asyncio.get_running_loop()
                attempts: list[asyncio.Task[Any]] = []  # in start order
                running: set[asyncio.Task[Any]] = set()
                timer: asyncio.Task[None] | None = None
                last_exc: BaseException | None = None

                def launch() -> None:
                    nonlocal timer
                    task = loop.create_task(attempt(args, kwargs))
                    attempts.append(task)
                    running.add(task)
                    if timer is not None:
                        timer.cancel()
                    timer = (
                        loop.create_task(_core.async_sleep(delay))
                        if len(attempts) < max_attempts
                        else None
                    )

                try:
                    launch()
                    while running:
                        waiting: set[asyncio.Future[Any]] = set(running)
                        if timer is not None:
                            waiting.add(timer)
                        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                        for task in [t for t in attempts if t in done]:
                            running.discard(task)
                            if task.cancelled():  # cancelled by someone else
                                raise asyncio.CancelledError
                            exc = task.exception()
                            if exc is None:
                                return task.result()
                            if not isinstance(exc, retry_on):
                                raise exc
                            last_exc = exc
                            if len(attempts) < max_attempts:
                                launch()
                        if timer is not None and timer in done:
                            timer = None
                            if len(attempts) < max_attempts:
                                launch()
                    assert last_exc is not None
                    raise last_exc
                finally:
                    leftovers: list[asyncio.Future[Any]] = list(running)
                    if timer is not None:
                        leftovers.append(timer)
                    for t in leftovers:
                        t.cancel()
                    if leftovers:
                        await asyncio.wait(leftovers)
                        for t in leftovers:
                            if not t.cancelled():
                                t.exception()  # retrieve: a loser's late error is moot

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempts: list[concurrent.futures.Future[Any]] = [_start_thread(func, args, kwargs)]
            running = set(attempts)
            last_exc: BaseException | None = None
            timeout: float | None = delay if max_attempts > 1 else None
            while running:
                done, _ = concurrent.futures.wait(
                    running, timeout=timeout, return_when=concurrent.futures.FIRST_COMPLETED
                )
                launches = 0 if done else 1  # nothing finished: the delay expired
                for future in [f for f in attempts if f in done]:
                    running.discard(future)
                    exc = future.exception()
                    if exc is None:
                        return future.result()
                    if not isinstance(exc, retry_on):
                        raise exc
                    last_exc = exc
                    launches += 1
                for _ in range(min(launches, max_attempts - len(attempts))):
                    future = _start_thread(func, args, kwargs)
                    attempts.append(future)
                    running.add(future)
                timeout = delay if len(attempts) < max_attempts else None
            assert last_exc is not None
            raise last_exc

        return cast(F, wrapper)

    return decorate
