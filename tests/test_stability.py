"""Pins the declared stable core (docs/project/stability.md).

A failure here means a stable-core name, parameter or default changed. That needs a
CHANGELOG entry under *Changed* or *Breaking changes* and an update to the stability page;
it is never a test to "fix" by editing the expectation alone.
"""

from __future__ import annotations

import inspect

import pytest

import python_wrap
from python_wrap import errors, presets
from python_wrap.events import EVENT_NAMES
from python_wrap.policy import ROLES

STABLE_NAMES = [
    "retry", "retrying", "RetryBudget", "RetryState", "retry_after_header", "http_status",
    "attempt_history", "AttemptRecord", "CircuitBreaker", "CircuitState", "circuit_breaker",
    "CircuitOpenError", "timeout", "Timeout", "deadline", "TimeoutExpired", "policy", "Policy",
    "PythonWrapError",
]  # fmt: skip

# name -> {parameter: default} for the parameters whose defaults are frozen.
FROZEN_DEFAULTS = {
    "retry": {"on": Exception, "retry_if": None, "delays": None, "log_values": True},
    "CircuitBreaker": {
        "failure_threshold": 5,
        "recovery_timeout": 30.0,
        "half_open_max_calls": 1,
        "success_threshold": 1,
        "on": Exception,
    },
    "RetryBudget": {"ratio": 0.1, "min_per_second": 10, "window": 10.0},
}

PRESET_DEFAULTS = {
    "http_client": {"timeout_seconds": 10.0, "attempts": 3, "idempotent": True},
    "db_call": {"timeout_seconds": 5.0, "attempts": 3, "breaker": False},
    "third_party_api": {"timeout_seconds": 15.0, "attempts": 4, "period": 1.0},
}

STABLE_EVENTS = [
    "retry.attempt_failed", "retry.succeeded", "retry.gave_up", "retry.budget_exhausted",
    "timeout.expired", "circuit.state_changed", "circuit.rejected",
]  # fmt: skip


@pytest.mark.parametrize("name", STABLE_NAMES)
def test_stable_names_are_exported(name):
    assert name in python_wrap.__all__


@pytest.mark.parametrize(("name", "defaults"), FROZEN_DEFAULTS.items())
def test_frozen_defaults(name, defaults):
    target = getattr(python_wrap, name)
    params = inspect.signature(target).parameters
    for parameter, value in defaults.items():
        assert params[parameter].default == value, (name, parameter)


@pytest.mark.parametrize(("name", "defaults"), PRESET_DEFAULTS.items())
def test_preset_defaults(name, defaults):
    params = inspect.signature(getattr(presets, name)).parameters
    for parameter, value in defaults.items():
        assert params[parameter].default == value, (name, parameter)


def test_role_order_is_frozen():
    assert ROLES == (
        "observe", "validate", "fallback", "cache", "limiter", "bulkhead", "breaker", "retry",
        "timeout", "guard",
    )  # fmt: skip


@pytest.mark.parametrize("event", STABLE_EVENTS)
def test_stable_events_exist(event):
    assert event in EVENT_NAMES


def test_exception_hierarchy_is_frozen():
    assert issubclass(errors.TimeoutExpired, TimeoutError)
    assert issubclass(errors.ValidationError, ValueError)
    assert issubclass(errors.ArgumentTypeError, TypeError)
    for name in errors.__all__:
        assert issubclass(getattr(errors, name), errors.PythonWrapError)
    assert errors.CircuitOpenError("x", retry_after=1.0).retry_after == 1.0
    assert errors.RateLimitExceeded("x", retry_after=2.0).retry_after == 2.0


def test_retry_parameters_stay_keyword_only():
    params = inspect.signature(python_wrap.retry).parameters
    assert params["func"].kind is inspect.Parameter.POSITIONAL_ONLY
    keyword_only = {p for p, v in params.items() if v.kind is inspect.Parameter.KEYWORD_ONLY}
    assert {"max_attempts", "delay", "backoff", "max_delay", "jitter", "max_time"} <= keyword_only
