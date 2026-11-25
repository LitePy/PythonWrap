"""Cache and idempotency keys must not depend on the per-process hash seed."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from python_wrap._runtime import call_key, stable_digest

_SNIPPET = (
    "from python_wrap._runtime import stable_digest, call_key\n"
    "def f(a, b): ...\n"
    "print(stable_digest(({'b': 1, 'a': 2}, {'x', 'y', 'z'}, frozenset({'p', 'q'}))))\n"
    "print(call_key(f, None, ({'b': 1, 'a': 2}, {'x', 'y'}), {}))\n"
)


def _run(seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [sys.executable, "-c", _SNIPPET], env=env, capture_output=True, text=True, check=True
    ).stdout


def test_same_key_under_different_hash_seeds() -> None:
    assert _run("1") == _run("2") == _run("12345")


def test_dict_order_and_set_order_do_not_matter() -> None:
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})
    assert stable_digest([1, 2]) != stable_digest((1, 2))
    assert stable_digest({1}) != stable_digest([1])


def test_unstable_argument_is_rejected_with_its_name() -> None:
    def f(x: object) -> None: ...

    with pytest.raises(TypeError, match=r"argument 'x'.*key="):
        call_key(f, None, (object(),), {})
