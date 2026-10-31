"""Redact secrets from log arguments, audit payloads and nested data structures.

Names are matched by **segment** by default: ``author`` and ``authorised_by`` are not
secrets just because they contain ``auth``. A name is split on underscores, hyphens,
spaces, digits and camelCase boundaries, and a fragment matches when it equals a whole
segment (or its simple plural). Multi-word fragments such as ``api_key`` match a run of
consecutive segments. Pass ``match="substring"`` for the old, looser behaviour.

`redact` walks nested mappings, lists, tuples and sets, so request bodies and audit
payloads can be masked with the same rules as `log_args`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["DEFAULT_REDACT", "is_sensitive", "redact", "segments"]

#: Fragments considered sensitive by `log_args`, `audit`, `redact` and `trace`.
#: Matched by whole name segment, so ``token`` covers ``auth_token`` but not ``tokenizer``.
DEFAULT_REDACT: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "auth_token",
    "auth_key",
    "auth_secret",
    "credential",
    "private_key",
    "cookie",
    "session",
    "session_token",
    "session_key",
    "session_secret",
)

#: Built-in words that are too ambiguous to match inside a longer name: ``auth`` masks a
#: bare ``auth`` argument, but not ``author``, ``author_id`` or ``authorised_by``;
#: ``session`` masks ``session`` but not ``session_id``, ``session_date`` or
#: ``study_session``. Compound names such as ``auth_token`` are listed explicitly in
#: `DEFAULT_REDACT`. Custom ``redact=`` fragments are matched normally.
_EXACT_ONLY = frozenset({"auth", "session"})

MASK = "***"

_SEPARATORS = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def segments(name: str) -> tuple[str, ...]:
    """Split a name into lowercase word segments.

    ``"sessionID"``, ``"session_id"`` and ``"Session-ID"`` all become
    ``("session", "id")``. Non-alphanumeric runs are separators; an empty name has no
    segments.

    Example:
        >>> segments("HTTPApiKey_2")
        ('http', 'api', 'key', '2')
    """
    spaced = _CAMEL.sub(" ", name)
    spaced = re.sub(r"(?<=[a-zA-Z])(?=[0-9])|(?<=[0-9])(?=[a-zA-Z])", " ", spaced)
    return tuple(part for part in _SEPARATORS.split(spaced.lower()) if part)


def _fragment_parts(fragment: str) -> tuple[str, ...]:
    return segments(fragment)


def _segment_matches(segment: str, fragment: str) -> bool:
    if segment == fragment:
        return True
    # A plural of a one-word fragment: tokens, credentials, cookies.
    return segment in (f"{fragment}s", f"{fragment}es")


def _contains_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[i : i + width] == needle for i in range(len(haystack) - width + 1))


def is_sensitive(
    name: object,
    fragments: Iterable[str] | None = None,
    *,
    match: str = "segment",
) -> bool:
    """Return True when ``name`` looks like it holds a secret.

    Args:
        name: A parameter name or mapping key.
        fragments: Fragments to match. Defaults to `DEFAULT_REDACT`; an empty iterable
            disables redaction.
        match: ``"segment"`` (default) matches whole word segments; ``"substring"``
            matches anywhere in the name, case-insensitively.

    Raises:
        ValueError: If ``match`` is neither ``"segment"`` nor ``"substring"``.

    Example:
        >>> is_sensitive("author")
        False
        >>> is_sensitive("author_id")
        False
        >>> is_sensitive("auth_token")
        True
        >>> is_sensitive("apiKey")
        True
    """
    if match not in ("segment", "substring"):
        raise ValueError(f"match must be 'segment' or 'substring', got {match!r}")
    text = str(name)
    chosen = DEFAULT_REDACT if fragments is None else tuple(fragments)
    if not chosen:
        return False
    if match == "substring":
        lowered = text.lower()
        return any(fragment.lower() in lowered for fragment in chosen if fragment)
    parts = segments(text)
    if not parts:
        return False
    joined = "".join(parts)
    for fragment in chosen:
        if not fragment:
            continue
        wanted = _fragment_parts(fragment)
        if not wanted:
            continue
        if joined == "".join(wanted):
            # "apikey" as a single segment matches the "api_key" fragment.
            return True
        if fragment.lower() in _EXACT_ONLY and all(item in chosen for item in DEFAULT_REDACT):
            # Ambiguous words only mask the bare name (or its plural), never a compound.
            if joined in (fragment.lower(), f"{fragment.lower()}s", f"{fragment.lower()}es"):
                return True
            continue
        if len(wanted) == 1:
            if any(_segment_matches(part, wanted[0]) for part in parts):
                return True
        elif _contains_run(parts, wanted):
            return True
    return False


def redact(
    obj: Any,
    *,
    fragments: Iterable[str] | None = None,
    match: str = "segment",
    max_depth: int = 12,
    mask: str = MASK,
) -> Any:
    """Return a copy of ``obj`` with sensitive values replaced by ``mask``.

    Mappings are copied key by key: a key that `is_sensitive` masks its value outright,
    including nested containers. Lists, tuples, sets and frozensets are rebuilt with the
    same type where possible. Everything else is returned unchanged (no attempt is made
    to introspect arbitrary objects). Recursion stops at ``max_depth``; deeper containers
    are masked entirely, and cycles are masked, so truncation cannot expose secrets.

    Args:
        obj: The value to copy.
        fragments: Sensitive name fragments; defaults to `DEFAULT_REDACT`.
        match: ``"segment"`` (default) or ``"substring"``; see `is_sensitive`.
        max_depth: How many container levels to walk (>= 0).
        mask: Replacement value for masked entries.

    Raises:
        ValueError: If ``max_depth`` is negative or ``match`` is invalid.

    Example:
        >>> redact({"user": "bob", "auth": {"token": "t"}, "rows": [{"password": "p"}]})
        {'user': 'bob', 'auth': '***', 'rows': [{'password': '***'}]}
    """
    if max_depth < 0:
        raise ValueError(f"max_depth must be >= 0, got {max_depth!r}")
    if match not in ("segment", "substring"):
        raise ValueError(f"match must be 'segment' or 'substring', got {match!r}")
    chosen = DEFAULT_REDACT if fragments is None else tuple(fragments)
    return _walk(obj, chosen, match, max_depth, mask, 0, set())


def _walk(
    obj: Any,
    fragments: tuple[str, ...],
    match: str,
    max_depth: int,
    mask: str,
    depth: int,
    ancestors: set[int],
) -> Any:
    container = isinstance(obj, (Mapping, list, tuple, set, frozenset))
    if container and (depth > max_depth or id(obj) in ancestors):
        return mask
    if not container:
        return obj
    ancestors = ancestors | {id(obj)}
    if isinstance(obj, Mapping):
        return {
            key: (
                mask
                if is_sensitive(key, fragments, match=match)
                else _walk(value, fragments, match, max_depth, mask, depth + 1, ancestors)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [
            _walk(item, fragments, match, max_depth, mask, depth + 1, ancestors) for item in obj
        ]
    if isinstance(obj, tuple):
        return tuple(
            _walk(item, fragments, match, max_depth, mask, depth + 1, ancestors) for item in obj
        )
    if isinstance(obj, (set, frozenset)):
        return type(obj)(
            _walk(item, fragments, match, max_depth, mask, depth + 1, ancestors) for item in obj
        )
    return obj
