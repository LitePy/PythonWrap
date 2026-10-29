from __future__ import annotations

import logging
from typing import Any

import pytest

from python_wrap.logs import audit, log_args
from python_wrap.redaction import DEFAULT_REDACT, is_sensitive, redact, segments


def test_segments_splits_snake_camel_and_punctuation() -> None:
    assert segments("session_id") == ("session", "id")
    assert segments("sessionID") == ("session", "id")
    assert segments("HTTPApiKey") == ("http", "api", "key")
    assert segments("training-session") == ("training", "session")
    assert segments("") == ()


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "user_password",
        "api_key",
        "apiKey",
        "auth_token",
        "session_token",
        "credentials",
    ],
)
def test_sensitive_names(name: str) -> None:
    assert is_sensitive(name)


@pytest.mark.parametrize(
    "name",
    [
        "author",
        "author_id",
        "authorised_by",
        "session_id",
        "session_date",
        "training_session",
        "tokenizer",
    ],
)
def test_harmless_names_are_not_masked(name: str) -> None:
    assert not is_sensitive(name)


def test_bare_auth_and_session_still_masked() -> None:
    assert is_sensitive("auth")
    assert is_sensitive("session")
    assert is_sensitive("auths")


def test_custom_fragments_use_segment_matching() -> None:
    assert is_sensitive("user_ssn", ["ssn"])
    assert not is_sensitive("ssnapshot", ["ssn"])
    assert is_sensitive("ssnapshot", ["ssn"], match="substring")


def test_empty_fragments_disable_matching() -> None:
    assert not is_sensitive("password", [])


def test_invalid_match() -> None:
    with pytest.raises(ValueError):
        is_sensitive("password", match="regex")


def test_redact_walks_nested_containers() -> None:
    payload: dict[str, Any] = {
        "user": "bob",
        "author": "alice",
        "session_id": "s1",
        "auth": "a",
        "nested": {"password": "p", "items": [{"token": "t"}, {"ok": 1}]},
        "tuple": ({"secret": "s"},),
        "set": {"keep"},
    }
    out = redact(payload)
    assert out["user"] == "bob"
    assert out["author"] == "alice"
    assert out["session_id"] == "s1"
    assert out["auth"] == "***"
    assert out["nested"] == {"password": "***", "items": [{"token": "***"}, {"ok": 1}]}
    assert out["tuple"] == ({"secret": "***"},)
    assert out["set"] == {"keep"}
    # The input is untouched.
    assert payload["nested"]["password"] == "p"


def test_redact_depth_limit_and_validation() -> None:
    payload = {"a": {"b": {"password": "p"}}}
    assert redact(payload, max_depth=1) == {"a": {"b": "***"}}
    with pytest.raises(ValueError):
        redact(payload, max_depth=-1)


def test_redact_custom_mask_and_fragments() -> None:
    assert redact({"ssn": "1", "password": "p"}, fragments=["ssn"], mask="###") == {
        "ssn": "###",
        "password": "p",
    }


def test_log_args_segment_matching_end_to_end(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @log_args(level=logging.INFO)
    def f(author: str, author_id: int, session_id: str, auth_token: str, auth: str) -> None: ...

    f("alice", 7, "s1", "tok", "bare")
    msg = caplog.records[0].getMessage()
    assert "author='alice'" in msg
    assert "author_id=7" in msg
    assert "session_id='s1'" in msg
    assert "auth_token='***'" in msg
    assert "auth='***'" in msg
    assert "password" in DEFAULT_REDACT


def test_log_args_substring_mode_is_opt_in(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="python_wrap")

    @log_args(level=logging.INFO, redact_match="substring")
    def f(author: str) -> None: ...

    f("alice")
    assert "author='***'" in caplog.records[0].getMessage()


def test_audit_redact_match_option() -> None:
    records: list[Any] = []

    @audit(redact_match="substring", sink=records.append)
    def f(author: str) -> None: ...

    f("alice")
    assert records[0].arguments == {"author": "***"}


def test_cycles_depth_and_explicit_custom_segments_do_not_leak():
    payload = {"password": "secret"}
    payload["cycle"] = payload
    assert redact(payload) == {"password": "***", "cycle": "***"}
    assert is_sensitive("training_session", ["session"])
    assert is_sensitive("password2")
    assert is_sensitive("v2Password")


def test_deep_log_and_audit_use_custom_fragments_without_mutation(caplog):
    caplog.set_level(logging.INFO, logger="python_wrap")
    records = []

    @audit(deep=True, redact_extra=["ssn"], sink=records.append)
    @log_args(deep=True, redact_extra=["ssn"], level=logging.INFO)
    def work(payload):
        return 7

    payload = {"author": "ana", "rows": [{"password": "hidden-pass", "ssn": "hidden-id"}]}
    assert work(payload) == 7
    assert records[0].arguments == {
        "payload": {"author": "ana", "rows": [{"password": "***", "ssn": "***"}]}
    }
    assert "hidden-pass" not in caplog.text and "hidden-id" not in caplog.text
    assert payload["rows"][0]["password"] == "hidden-pass"
