"""Tests for the tenacity/backoff codemod in ``python_wrap.migrate``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from python_wrap.migrate import main, migrate_source

TENACITY_IMPORT = (
    "from tenacity import retry, stop_after_attempt, stop_after_delay, wait_fixed, "
    "wait_exponential, wait_random, wait_exponential_jitter, retry_if_exception_type, "
    "retry_if_result, retry_if_exception, wait_none, stop_never\n"
)


def _rewrite(decorator: str, imports: str = TENACITY_IMPORT) -> str:
    source = f"{imports}\n{decorator}\ndef f(): ...\n"
    result = migrate_source(source)
    assert not result.unsupported, result.unsupported
    assert len(result.changes) == 1
    lines = result.text.splitlines()
    # everything between the import line and "def f" is the decorator
    start = next(i for i, ln in enumerate(lines) if ln.startswith("@"))
    end = lines.index("def f(): ...")
    return "\n".join(lines[start:end])


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        (
            "@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))",
            "@retry(max_attempts=3, delay=2, backoff=1)",
        ),
        (
            "@retry(stop=stop_after_attempt(5) | stop_after_delay(30), wait=wait_fixed(1))",
            "@retry(max_attempts=5, delay=1, backoff=1, max_time=30)",
        ),
        (
            "@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, max=60))",
            "@retry(max_attempts=4, delay=2, backoff=2, max_delay=60)",
        ),
        (
            "@retry(stop=stop_after_attempt(4), "
            "wait=wait_exponential(multiplier=1, min=4, max=10))",
            "@retry(max_attempts=4, delay=4, backoff=2, max_delay=10)",
        ),
        (
            "@retry(stop=stop_after_attempt(4), wait=wait_exponential(exp_base=3))",
            "@retry(max_attempts=4, delay=1, backoff=3)",
        ),
        (
            "@retry(stop=stop_after_attempt(2), wait=wait_random(1, 3))",
            "@retry(max_attempts=2, delay=1, backoff=1, jitter=2)",
        ),
        (
            "@retry(stop=stop_after_attempt(2), wait=wait_fixed(2) + wait_random(0, 1))",
            "@retry(max_attempts=2, delay=2, backoff=1, jitter=1)",
        ),
        (
            "@retry(stop=stop_after_attempt(2), wait=wait_fixed(2) + wait_random(1, 3))",
            "@retry(max_attempts=2, delay=3, backoff=1, jitter=2)",
        ),
        (
            "@retry(stop=stop_after_attempt(6), "
            "wait=wait_exponential_jitter(initial=0.5, max=20, jitter=2))",
            "@retry(max_attempts=6, delay=0.5, backoff=2, max_delay=20, jitter=2)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), wait=wait_none())",
            "@retry(max_attempts=3, delay=0, backoff=1)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), wait=wait_fixed(1), "
            "retry=retry_if_exception_type(ConnectionError))",
            "@retry(on=ConnectionError, max_attempts=3, delay=1, backoff=1)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), retry=retry_if_exception_type((A, B)))",
            "@retry(on=(A, B), max_attempts=3, delay=0, backoff=1)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), retry=retry_if_exception(is_transient))",
            "@retry(max_attempts=3, delay=0, backoff=1, retry_if=is_transient)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), retry=retry_if_result(lambda r: r is None))",
            "@retry(on=(), max_attempts=3, delay=0, backoff=1, "
            "retry_if_result=lambda r: r is None)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), retry=retry_if_exception_type(OSError) "
            "| retry_if_result(is_bad))",
            "@retry(on=OSError, max_attempts=3, delay=0, backoff=1, retry_if_result=is_bad)",
        ),
        (
            "@retry(stop=stop_after_attempt(3), reraise=True)",
            "@retry(max_attempts=3, delay=0, backoff=1)",
        ),
        (
            "@retry(wait=wait_fixed(1))",
            "@retry(max_attempts=None, delay=1, backoff=1)",
        ),
        ("@retry", "@retry(max_attempts=None, delay=0, backoff=1)"),
        ("@retry()", "@retry(max_attempts=None, delay=0, backoff=1)"),
        (
            "@retry(stop=stop_never, wait=wait_fixed(1))",
            "@retry(max_attempts=None, delay=1, backoff=1)",
        ),
    ],
)
def test_tenacity_rewrites(decorator: str, expected: str) -> None:
    assert _rewrite(decorator) == expected


def test_tenacity_module_attribute_and_alias_forms() -> None:
    source = (
        "import tenacity\n"
        "\n"
        "@tenacity.retry(stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_fixed(1))\n"
        "def f(): ...\n"
    )
    result = migrate_source(source)
    assert result.text == (
        "from python_wrap import retry\n"
        "\n"
        "@retry(max_attempts=3, delay=1, backoff=1)\n"
        "def f(): ...\n"
    )
    aliased = migrate_source(
        "import tenacity as t\nfrom tenacity import stop_after_attempt as sa\n"
        "@t.retry(stop=sa(2))\ndef f(): ...\n"
    )
    assert "@retry(max_attempts=2, delay=0, backoff=1)" in aliased.text
    assert "tenacity" not in aliased.text


def test_async_and_method_decorators_are_rewritten() -> None:
    source = (
        "from tenacity import retry, stop_after_attempt\n"
        "class C:\n"
        "    @retry(stop=stop_after_attempt(2))\n"
        "    async def go(self): ...\n"
    )
    result = migrate_source(source)
    assert "    @retry(max_attempts=2, delay=0, backoff=1)\n    async def go" in result.text
    assert result.text.startswith("from python_wrap import retry\n")


def test_long_decorator_is_wrapped() -> None:
    source = (
        "from tenacity import retry, stop_after_attempt, wait_exponential\n"
        "class C:\n"
        "    @retry(stop=stop_after_attempt(5), "
        "wait=wait_exponential(multiplier=1, min=4, "
        "max=SOME_VERY_LONG_CONSTANT_NAME_FOR_MAX_DELAY_SECONDS))\n"
        "    def go(self): ...\n"
    )
    result = migrate_source(source)
    assert not result.unsupported
    assert "    @retry(\n        max_attempts=5,\n" in result.text
    ast.parse(result.text)


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        (
            "@backoff.on_exception(backoff.expo, ConnectionError, max_tries=5, jitter=None)",
            "@retry(on=ConnectionError, max_attempts=5, delay=1, backoff=2)",
        ),
        (
            "@backoff.on_exception(backoff.expo, (A, B), max_tries=3, max_time=60, "
            "factor=2, base=3, max_value=30, jitter=None)",
            "@retry(on=(A, B), max_attempts=3, delay=2, backoff=3, max_delay=30, max_time=60)",
        ),
        (
            "@backoff.on_exception(backoff.constant, OSError, interval=4, max_tries=2, "
            "jitter=None)",
            "@retry(on=OSError, max_attempts=2, delay=4, backoff=1)",
        ),
        (
            "@backoff.on_exception(backoff.expo, OSError, jitter=None)",
            "@retry(on=OSError, max_attempts=None, delay=1, backoff=2)",
        ),
        (
            "@backoff.on_predicate(backoff.constant, interval=3, max_tries=4, jitter=None)",
            "@retry(on=(), max_attempts=4, delay=3, backoff=1, "
            "retry_if_result=lambda result: not result)",
        ),
        (
            "@backoff.on_predicate(backoff.expo, lambda r: r.status == 429, max_tries=4, "
            "jitter=None)",
            "@retry(on=(), max_attempts=4, delay=1, backoff=2, "
            "retry_if_result=lambda r: r.status == 429)",
        ),
    ],
)
def test_backoff_rewrites(decorator: str, expected: str) -> None:
    assert _rewrite(decorator, "import backoff\n") == expected


def test_backoff_default_jitter_adds_a_note() -> None:
    result = migrate_source(
        "import backoff\n@backoff.on_exception(backoff.expo, OSError, max_tries=3)\ndef f(): ...\n"
    )
    assert result.changes[0].notes
    assert "full_jitter" in result.changes[0].notes[0]


def test_backoff_from_import_form() -> None:
    result = migrate_source(
        "from backoff import on_exception, expo\n"
        "@on_exception(expo, OSError, max_tries=2, jitter=None)\n"
        "def f(): ...\n"
    )
    assert result.text.startswith("from python_wrap import retry\n")
    assert "@retry(on=OSError, max_attempts=2, delay=1, backoff=2)" in result.text


@pytest.mark.parametrize(
    "decorator",
    [
        "@retry(stop=stop_after_attempt(3), before_sleep=log_it)",
        "@retry(stop=stop_after_attempt(3), after=log_it)",
        "@retry(stop=stop_after_attempt(3) & stop_after_delay(5))",
        "@retry(stop=my_stop)",
        "@retry(stop=stop_after_attempt(3), wait=my_wait)",
        "@retry(wait=wait_fixed(1) + wait_fixed(2))",
        "@retry(wait=wait_exponential_jitter() + wait_random(0, 1))",
        "@retry(retry=retry_if_exception_type(A) & retry_if_result(p))",
        "@retry(retry=my_retry_condition)",
        "@retry(retry_error_callback=cb)",
        "@retry(sleep=my_sleep)",
        "@retry(*ARGS)",
        "@retry(**OPTS)",
    ],
)
def test_unsupported_tenacity_is_left_untouched_and_reported(decorator: str) -> None:
    source = f"{TENACITY_IMPORT}\n{decorator}\ndef f(): ...\n"
    result = migrate_source(source)
    assert result.text == source
    assert result.changes == []
    assert len(result.unsupported) >= 1
    finding = result.unsupported[0]
    assert finding.line == 3
    assert finding.col == 1  # the decorator expression, just after the "@"
    assert finding.message


@pytest.mark.parametrize(
    "decorator",
    [
        "@backoff.on_exception(backoff.fibo, OSError)",
        "@backoff.on_exception(my_gen, OSError)",
        "@backoff.on_exception(backoff.expo, OSError, giveup=lambda e: True)",
        "@backoff.on_exception(backoff.expo, OSError, on_backoff=log)",
        "@backoff.on_exception(backoff.expo, OSError, raise_on_giveup=False)",
        "@backoff.on_exception(backoff.expo, OSError, jitter=my_jitter)",
        "@backoff.on_exception(backoff.expo, OSError, max_tries=lambda: 3)",
        "@backoff.on_exception(backoff.expo)",
    ],
)
def test_unsupported_backoff_is_left_untouched(decorator: str) -> None:
    source = f"import backoff\n{decorator}\ndef f(): ...\n"
    result = migrate_source(source)
    assert result.text == source
    assert result.unsupported
    assert result.unsupported[0].line == 2


def test_partial_file_rewrites_supported_and_reports_the_rest() -> None:
    source = (
        "from tenacity import retry, stop_after_attempt\n"
        "\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def good(): ...\n"
        "\n"
        "@retry(stop=stop_after_attempt(2), before_sleep=hook)\n"
        "def bad(): ...\n"
    )
    result = migrate_source(source)
    assert len(result.changes) == 1
    assert len(result.unsupported) == 1
    # tenacity's retry is still needed by `bad`, so the new one is imported under an alias
    assert "from python_wrap import retry as python_wrap_retry\n" in result.text
    assert "from tenacity import retry, stop_after_attempt\n" in result.text
    assert "@python_wrap_retry(max_attempts=2, delay=0, backoff=1)" in result.text
    assert "@retry(stop=stop_after_attempt(2), before_sleep=hook)" in result.text


# ---------------------------------------------------------------------------- imports


def test_unused_tenacity_names_are_removed_and_used_ones_kept() -> None:
    source = (
        "from tenacity import (\n"
        "    RetryError,\n"
        "    retry,\n"
        "    stop_after_attempt,\n"
        ")\n"
        "\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): ...\n"
        "\n"
        "try:\n"
        "    f()\n"
        "except RetryError:\n"
        "    pass\n"
    )
    result = migrate_source(source)
    assert result.text.startswith(
        "from tenacity import RetryError\nfrom python_wrap import retry\n"
    )
    assert [u.message for u in result.unsupported] == [
        "tenacity.RetryError is still used and has no automatic migration; see the migration guide"
    ]
    assert result.unsupported[0].line == 12


def test_existing_python_wrap_import_is_reused() -> None:
    source = (
        "from python_wrap import retry as pw_retry\n"
        "from tenacity import retry, stop_after_attempt\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): ...\n"
    )
    result = migrate_source(source)
    assert result.text == (
        "from python_wrap import retry as pw_retry\n"
        "@pw_retry(max_attempts=2, delay=0, backoff=1)\n"
        "def f(): ...\n"
    )


def test_local_name_clash_uses_an_alias() -> None:
    source = (
        "from tenacity import retry, stop_after_attempt\n"
        "from other import something\n"
        "def retry_helper(): ...\n"
        "retry_alias = 1\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): ...\n"
    )
    result = migrate_source(source)
    assert "from python_wrap import retry\n" in result.text


def test_star_import_is_reported() -> None:
    source = "from tenacity import *\n\n@retry(stop=stop_after_attempt(2))\ndef f(): ...\n"
    result = migrate_source(source)
    assert result.text == source
    assert any("star import" in u.message for u in result.unsupported)


def test_non_decorator_uses_are_reported_not_rewritten() -> None:
    source = (
        "from tenacity import Retrying, stop_after_attempt\n"
        "for attempt in Retrying(stop=stop_after_attempt(2)):\n"
        "    pass\n"
    )
    result = migrate_source(source)
    assert result.text == source
    assert {u.message.split(" ")[0] for u in result.unsupported} == {
        "tenacity.Retrying",
        "tenacity.stop_after_attempt",
    }


def test_unrelated_files_are_untouched() -> None:
    source = "import os\n\n\ndef retry(x):\n    return x\n\n\n@retry\ndef f(): ...\n"
    result = migrate_source(source)
    assert result.text == source and not result.changes and not result.unsupported


def test_crlf_and_encoding_survive() -> None:
    source = (
        "from tenacity import retry, stop_after_attempt\r\n"
        "@retry(stop=stop_after_attempt(2))\r\n"
        'def f(): return "caf\u00e9"\r\n'
    )
    result = migrate_source(source)
    assert result.text == (
        "from python_wrap import retry\r\n"
        "@retry(max_attempts=2, delay=0, backoff=1)\r\n"
        'def f(): return "caf\u00e9"\r\n'
    )


def test_non_ascii_before_decorator_keeps_offsets_right() -> None:
    source = (
        "# \u00fcber caf\u00e9\n"
        "from tenacity import retry, stop_after_attempt\n"
        "x = '\u00e9\u00e9'; y = 1\n"
        "@retry(stop=stop_after_attempt(2))  # \u00e9\n"
        "def f(): ...\n"
    )
    result = migrate_source(source)
    assert "@retry(max_attempts=2, delay=0, backoff=1)  # \u00e9\n" in result.text


def test_multiple_decorators_and_stacking() -> None:
    source = (
        "import functools\n"
        "from tenacity import retry, stop_after_attempt\n"
        "@functools.cache\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): ...\n"
        "@retry(stop=stop_after_attempt(3))\n"
        "def g(): ...\n"
    )
    result = migrate_source(source)
    assert len(result.changes) == 2
    assert result.text.count("from python_wrap import retry") == 1
    ast.parse(result.text)


# ---------------------------------------------------------------------------- robustness


def test_invalid_python_is_reported_not_raised() -> None:
    result = migrate_source("def broken(:\n    pass\n")
    assert result.text == "def broken(:\n    pass\n"
    assert len(result.unsupported) == 1
    assert "cannot parse" in result.unsupported[0].message
    assert result.unsupported[0].line == 1


def test_output_is_always_valid_python() -> None:
    result = migrate_source(
        "from tenacity import retry, stop_after_attempt, wait_fixed\n"
        "@retry(stop=stop_after_attempt(2), wait=wait_fixed(\n    1.5\n))\n"
        "def f(): ...\n"
    )
    ast.parse(result.text)
    assert "delay=1.5" in result.text


def test_idempotent() -> None:
    source = (
        "import backoff\n"
        "from tenacity import retry, stop_after_attempt, wait_fixed, Retrying\n"
        "@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))\n"
        "def f(): ...\n"
        "@backoff.on_exception(backoff.expo, OSError, max_tries=3, jitter=None)\n"
        "def g(): ...\n"
        "@retry(before_sleep=hook)\n"
        "def h(): ...\n"
    )
    once = migrate_source(source)
    twice = migrate_source(once.text)
    assert once.changes
    assert twice.text == once.text
    assert twice.changes == []


def test_changed_decorator_is_valid_python_runtime() -> None:
    from python_wrap import retry

    namespace: dict[str, object] = {"retry": retry}
    result = migrate_source(
        "from tenacity import retry, stop_after_attempt\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): return 1\n"
    )
    code = "\n".join(
        line for line in result.text.splitlines() if not line.startswith("from python_wrap")
    )
    exec(code, namespace)
    assert callable(namespace["f"])


# ---------------------------------------------------------------------------- CLI

SAMPLE = (
    "from tenacity import retry, stop_after_attempt, wait_fixed\n"
    "\n"
    "@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))\n"
    "def f(): ...\n"
)
REWRITTEN = (
    "from python_wrap import retry\n\n@retry(max_attempts=3, delay=2, backoff=1)\ndef f(): ...\n"
)


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(SAMPLE)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text(SAMPLE)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "skip2.py").write_text(SAMPLE)
    (tmp_path / "clean.py").write_text("x = 1\n")
    return tmp_path


def test_cli_dry_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path)
    assert main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "would rewrite 1 decorator(s)" in out
    assert "a.py" in out and "skip.py" not in out and "skip2.py" not in out
    assert (root / "pkg" / "a.py").read_text() == SAMPLE


def test_cli_diff_prints_unified_diff_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    assert main(["--diff", str(root)]) == 0
    out = capsys.readouterr().out
    assert "--- a/" in out and "+++ b/" in out
    assert "-from tenacity import" in out
    assert "+@retry(max_attempts=3, delay=2, backoff=1)" in out
    assert (root / "pkg" / "a.py").read_text() == SAMPLE


def test_cli_write_rewrites_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    assert main(["--write", str(root)]) == 0
    assert (root / "pkg" / "a.py").read_text() == REWRITTEN
    assert (root / ".venv" / "skip.py").read_text() == SAMPLE
    capsys.readouterr()
    assert main(["--write", str(root)]) == 0
    assert (root / "pkg" / "a.py").read_text() == REWRITTEN
    assert "rewrote 0 file(s)" in capsys.readouterr().out


def test_cli_accepts_single_files(tmp_path: Path) -> None:
    target = tmp_path / "one.py"
    target.write_text(SAMPLE)
    assert main(["--write", str(target)]) == 0
    assert target.read_text() == REWRITTEN


def test_cli_exit_code_one_on_unsupported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "bad.py"
    target.write_text("from tenacity import retry\n@retry(before_sleep=hook)\ndef f(): ...\n")
    assert main([str(target)]) == 1
    out = capsys.readouterr().out
    assert f"{target}:2:2: unsupported:" in out
    assert target.read_text().endswith("def f(): ...\n")


def test_cli_invalid_python_and_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n")
    assert main([str(bad)]) == 1
    assert "cannot parse" in capsys.readouterr().out
    assert bad.read_text() == "def (:\n"
    assert main([str(tmp_path / "missing.py")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_non_utf8_declared_encoding_roundtrips(tmp_path: Path) -> None:
    target = tmp_path / "latin.py"
    body = (
        "# -*- coding: latin-1 -*-\n"
        "from tenacity import retry, stop_after_attempt\n"
        "@retry(stop=stop_after_attempt(2))\n"
        "def f(): return 'caf\u00e9'\n"
    )
    target.write_bytes(body.encode("latin-1"))
    assert main(["--write", str(target)]) == 0
    assert "caf\u00e9".encode("latin-1") in target.read_bytes()
    assert b"max_attempts=2" in target.read_bytes()
