"""Project-wide defaults for python-wrap decorators.

Call `configure` once at application start-up, **before** the decorators you want to
affect are applied. Values are read when a decorator is *applied* (at import time for
module-level functions), so a decorator applied earlier keeps the settings it saw.
Arguments passed explicitly to a decorator always win over configured defaults.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import threading
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from python_wrap import _core

__all__ = ["configure", "configured", "get_config", "reset_config"]

UNSET: Any = object()
"""Sentinel meaning "argument not passed"; resolved against the configured defaults."""

_KEYS = ("logger", "retry", "redact", "redact_extra")
_RETRY_KEYS = (
    "max_attempts",
    "delay",
    "backoff",
    "max_delay",
    "jitter",
    "max_time",
    "log_level",
)

_lock = threading.RLock()
_state: dict[str, Any] = {}


def _hint(name: str, valid: tuple[str, ...], what: str) -> TypeError:
    close = difflib.get_close_matches(name, valid, n=1)
    tip = f" Did you mean {close[0]!r}?" if close else ""
    return TypeError(f"Unknown {what} {name!r}.{tip} Valid: {', '.join(valid)}.")


def _names(value: Any, key: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError(f"{key} must be an iterable of strings, got {value!r}")
    names = tuple(value)
    if not all(isinstance(n, str) for n in names):
        raise TypeError(f"{key} must contain only strings, got {value!r}")
    return names


def _validate(key: str, value: Any) -> Any:
    if key == "logger":
        if value is not None and not isinstance(value, (str, logging.Logger)):
            raise TypeError(f"logger must be a logging.Logger, a name or None, got {value!r}")
        return value
    if key == "retry":
        if not isinstance(value, Mapping):
            raise TypeError(f"retry must be a mapping of retry defaults, got {value!r}")
        for name in value:
            if name not in _RETRY_KEYS:
                raise _hint(str(name), _RETRY_KEYS, "retry default")
        from python_wrap.retry import _validate as validate_retry  # avoids an import cycle

        validate_retry(
            value.get("max_attempts", 3),
            value.get("delay", 1.0),
            value.get("backoff", 1.0),
            value.get("max_delay"),
            value.get("jitter", 0.0),
            value.get("max_time"),
        )
        return dict(value)
    if key == "redact":
        return None if value is None else _names(value, "redact")
    return _names(value, "redact_extra")


def configure(**defaults: Any) -> None:
    """Set project-wide defaults for python-wrap decorators.

    Call it once at start-up, before decorating: values are read when a decorator is
    applied, so decorators applied earlier keep their settings. Keys not mentioned keep
    their current value; each key you pass replaces that key entirely (`retry={...}`
    replaces the previous retry defaults). Explicit decorator arguments always win.

    Args:
        **defaults: Any of:

            - `logger`: logger (or name) used by every decorator that logs when it
              is not given `logger=`. `None` restores the package logger.
            - `retry`: dict of defaults for `retry`/`retrying`; keys `max_attempts`,
              `delay`, `backoff`, `max_delay`, `jitter`, `max_time`, `log_level`.
            - `redact`: default redaction fragments for `log_args` and `audit`
              (replaces `DEFAULT_REDACT`); `None` restores it.
            - `redact_extra`: fragments added to the redaction list (the built-ins
              stay), for `log_args` and `audit`.

    Raises:
        TypeError: For an unknown key (with a "did you mean" hint) or a wrongly typed value.
        ValueError: If a retry default is out of range.

    Example:
        ```python
        import logging
        from python_wrap.config import configure, reset_config

        configure(logger=logging.getLogger("app"), retry={"max_attempts": 5, "delay": 0.5})
        reset_config()
        ```
    """
    for key in defaults:
        if key not in _KEYS:
            raise _hint(key, _KEYS, "configuration key")
    clean = {k: _validate(k, v) for k, v in defaults.items()}
    with _lock:
        _state.update(clean)
        if "logger" in clean:
            _core._default_logger = clean["logger"]


def get_config() -> dict[str, Any]:
    """Return a copy of the current configuration.

    Returns:
        A dict with keys `logger`, `retry`, `redact` and `redact_extra`, holding the
        defaults in force (`None`, `{}`, `None` and `()` when nothing is configured).

    Example:
        ```python
        from python_wrap.config import get_config

        assert get_config()["retry"] == {}
        ```
    """
    with _lock:
        return {
            "logger": _state.get("logger"),
            "retry": dict(_state.get("retry", {})),
            "redact": _state.get("redact"),
            "redact_extra": _state.get("redact_extra", ()),
        }


def reset_config() -> None:
    """Forget every configured default, restoring the built-in behaviour.

    Example:
        ```python
        from python_wrap.config import configure, get_config, reset_config

        configure(retry={"max_attempts": 9})
        reset_config()
        assert get_config()["retry"] == {}
        ```
    """
    with _lock:
        _state.clear()
        _core._default_logger = None


@contextlib.contextmanager
def configured(**defaults: Any) -> Iterator[None]:
    """Apply `configure(**defaults)` for a `with` block, then restore the previous state.

    Meant for tests. Remember that decorators read the defaults when applied, so apply
    them inside the block.

    Args:
        **defaults: Same keys as `configure`.

    Example:
        ```python
        from python_wrap.config import configured, get_config

        with configured(retry={"max_attempts": 2}):
            assert get_config()["retry"] == {"max_attempts": 2}
        assert get_config()["retry"] == {}
        ```
    """
    with _lock:
        saved = dict(_state)
        configure(**defaults)
    try:
        yield
    finally:
        with _lock:
            _state.clear()
            _state.update(saved)
            _core._default_logger = saved.get("logger")


def retry_default(name: str, value: Any, fallback: Any) -> Any:
    """Resolve a retry argument: explicit value, else configured default, else `fallback`."""
    if value is not UNSET:
        return value
    with _lock:
        retry_defaults: dict[str, Any] = _state.get("retry", {})
        return retry_defaults.get(name, fallback)


def redaction(explicit: Iterable[str] | None, builtin: tuple[str, ...]) -> tuple[str, ...]:
    """Final redaction fragments: explicit list or configured/built-in, plus `redact_extra`."""
    with _lock:
        base: tuple[str, ...] | None = _state.get("redact")
        extra: tuple[str, ...] = _state.get("redact_extra", ())
    chosen = tuple(explicit) if explicit is not None else (builtin if base is None else base)
    return chosen + extra
