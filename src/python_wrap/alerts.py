"""Alert a chat webhook when a circuit breaker opens.

`WebhookAlerter` listens for circuit state changes and posts a short message to a
webhook (Slack, Microsoft Teams, or a generic JSON endpoint). Delivery happens on a
background daemon thread so the code path that tripped the breaker never waits on the
network, and a failed delivery is logged, never raised. Each breaker is rate limited by
a cooldown so a flapping dependency produces one alert, not hundreds.

Payloads contain only the breaker name, the states, a timestamp and the static
``context`` you supply. Exception messages, URLs and credentials are never included.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlsplit

from python_wrap import _core, _fork
from python_wrap._core import Event
from python_wrap.circuit import CircuitBreaker, CircuitState
from python_wrap.events import subscribe

__all__ = ["Sender", "WebhookAlerter"]

Sender = Callable[[str, bytes, Mapping[str, str]], None]
"""Signature of ``sender``: ``sender(url, body_bytes, headers)``. Raise to signal failure."""

_FORMATS = ("json", "teams", "slack")
_STATES = ("closed", "open", "half_open")
_log = logging.getLogger("python_wrap")


def _redact(url: str) -> str:
    """Keep only scheme and host: webhook paths and query strings are secrets."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or '?'}/..."


def _state_name(state: object) -> str:
    return str(getattr(state, "value", state))


class WebhookAlerter:
    """Post a chat message when a circuit breaker reaches a chosen state.

    Use it as an ``on_state_change`` listener, or call `install` to receive every
    breaker's ``circuit.state_changed`` event in the process. After an alert for a
    breaker was sent, its return to ``closed`` sends a recovery message (unless
    ``include_recovery=False``).

    Args:
        url: Webhook URL (``http`` or ``https``). Never logged in full.
        format: ``"json"`` (plain dict), ``"teams"`` (Adaptive Card message) or
            ``"slack"`` (``{"text": ...}``).
        on: States that trigger an alert, as strings or `CircuitState` values.
        cooldown: Minimum seconds between alerts for the same breaker.
        timeout: Network timeout in seconds for the default sender.
        sender: Replacement delivery function ``sender(url, body_bytes, headers)``,
            mainly for tests. It runs on a background thread. The default POSTs with
            `urllib.request`.
        include_recovery: Send a "closed again" message after an alerted open.
        context: Static key/values (service, environment, ...) added to every message.

    Raises:
        ValueError: For an unknown ``format`` or state, a non-http URL, or a
            negative ``cooldown`` / non-positive ``timeout``.

    Example:
        ```python
        from python_wrap import CircuitBreaker
        from python_wrap.alerts import WebhookAlerter

        sent = []
        alerter = WebhookAlerter(
            "https://chat.example.com/hooks/abc",
            format="slack",
            sender=lambda url, body, headers: sent.append(body),
            context={"service": "billing", "env": "prod"},
        )
        breaker = CircuitBreaker(failure_threshold=1, name="payments", on_state_change=alerter)

        @breaker
        def charge() -> None:
            raise ConnectionError("down")

        try:
            charge()
        except ConnectionError:
            pass
        assert alerter.flush(2)
        assert len(sent) == 1 and b"payments" in sent[0]
        ```
    """

    def __init__(
        self,
        url: str,
        *,
        format: str = "json",
        on: Iterable[str | CircuitState] = ("open",),
        cooldown: float = 300.0,
        timeout: float = 5.0,
        sender: Sender | None = None,
        include_recovery: bool = True,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("url must be an http or https URL")
        if format not in _FORMATS:
            raise ValueError(f"format must be one of {_FORMATS}")
        states = frozenset(_state_name(s) for s in on)
        if not states <= set(_STATES):
            raise ValueError(f"on must contain only {_STATES}")
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        self._url, self.format, self.on = url, format, states
        self.cooldown, self.timeout = float(cooldown), float(timeout)
        self.include_recovery = include_recovery
        self.context: dict[str, Any] = dict(context or {})
        self._sender: Sender = sender if sender is not None else self._post
        self._lock = _fork.ForkSafeLock()
        self._idle = threading.Condition(cast(Any, self._lock))
        self._last_alert: dict[str, float] = {}
        self._alerted: set[str] = set()
        self._pending = 0
        self._unsubscribe: Callable[[], None] | None = None
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._pending = 0
        self._unsubscribe = None

    # -- wiring ------------------------------------------------------------------------
    def __call__(self, breaker: CircuitBreaker, old: CircuitState, new: CircuitState) -> None:
        """``on_state_change`` listener entry point."""
        self.notify(breaker.name, _state_name(old), _state_name(new))

    def install(self) -> WebhookAlerter:
        """Subscribe to ``circuit.state_changed`` events for every breaker. Idempotent."""
        with self._lock:
            if self._unsubscribe is None:
                self._unsubscribe = subscribe(self._on_event)
        return self

    def close(self) -> None:
        """Unsubscribe from events. Pending deliveries still complete."""
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _on_event(self, event: Event) -> None:
        if event.name == "circuit.state_changed":
            self.notify(
                event.function, _state_name(event.fields["old"]), _state_name(event.fields["new"])
            )

    # -- decision ----------------------------------------------------------------------
    def notify(self, name: str, old: str, new: str) -> bool:
        """Decide whether a transition warrants a message, and queue it.

        Args:
            name: Breaker name.
            old: Previous state (``"closed"``, ``"open"`` or ``"half_open"``).
            new: New state.

        Returns:
            True if a message was queued.
        """
        kind: str | None = None
        with self._lock:
            now = _core.monotonic()
            if new in self.on:
                last = self._last_alert.get(name)
                if last is None or now - last >= self.cooldown:
                    self._last_alert[name] = now
                    self._alerted.add(name)
                    kind = "alert"
            elif new == "closed" and self.include_recovery and name in self._alerted:
                self._alerted.discard(name)
                kind = "recovery"
        if kind is None:
            return False
        self._dispatch(self._payload(kind, name, old, new))
        return True

    # -- payloads ----------------------------------------------------------------------
    def _payload(self, kind: str, name: str, old: str, new: str) -> dict[str, Any]:
        stamp = datetime.fromtimestamp(_core.wall_time(), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if kind == "recovery":
            text = f"Circuit breaker '{name}' is closed again"
        else:
            text = f"Circuit breaker '{name}' is now {new.upper()}"
        if self.format == "slack":
            lines = [text, f"time: {stamp}"]
            lines += [f"{k}: {v}" for k, v in self.context.items()]
            return {"text": "\n".join(lines)}
        if self.format == "teams":
            facts = [
                {"title": "Breaker", "value": name},
                {"title": "State", "value": new},
                {"title": "Time", "value": stamp},
                *({"title": str(k), "value": str(v)} for k, v in self.context.items()),
            ]
            card = {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": text, "weight": "Bolder", "wrap": True},
                    {"type": "FactSet", "facts": facts},
                ],
            }
            return {
                "type": "message",
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            }
        return {
            "text": text,
            "kind": kind,
            "breaker": name,
            "state": new,
            "previous_state": old,
            "time": stamp,
            "context": dict(self.context),
        }

    # -- delivery ----------------------------------------------------------------------
    def _dispatch(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode()
        with self._lock:
            self._pending += 1
        try:
            threading.Thread(
                target=self._deliver, args=(body,), daemon=True, name="python-wrap-alert"
            ).start()
        except BaseException:
            self._done()
            raise

    def _done(self) -> None:
        with self._idle:
            self._pending -= 1
            self._idle.notify_all()

    def _deliver(self, body: bytes) -> None:
        try:
            self._sender(self._url, body, {"Content-Type": "application/json"})
        except Exception as exc:
            # Only the exception type: messages from HTTP clients often embed the URL.
            _log.warning(
                "circuit alert delivery to %s failed (%s)", _redact(self._url), type(exc).__name__
            )
        finally:
            self._done()

    def _post(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        import urllib.request

        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until queued deliveries finish.

        Args:
            timeout: Seconds to wait (real time); ``None`` waits indefinitely.

        Returns:
            True if nothing is pending any more.
        """
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)
