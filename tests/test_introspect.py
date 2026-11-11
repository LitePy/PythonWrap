"""The API index, recipes, the packaged guide and their CLI commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

import python_wrap
from python_wrap import introspect
from python_wrap.__main__ import main
from python_wrap.introspect import (
    api_index,
    find_recipes,
    get_symbol,
    guide_text,
    recipes,
    search,
)

ROOT = Path(__file__).resolve().parent.parent


def test_index_covers_every_top_level_name_once() -> None:
    names = [entry.name for entry in api_index()]
    assert len(names) == len(set(names))
    assert set(python_wrap.__all__) <= set(names)


def test_index_includes_submodule_only_names_and_modules() -> None:
    by_name = {entry.name: entry for entry in api_index()}
    assert by_name["doctor.explain"].qualname == "python_wrap.doctor.explain"
    assert by_name["presets.http_client"].kind == "function"
    assert by_name["testing"].kind == "module"
    # Names re-exported at the top level are not repeated under their module.
    assert "retry.retry" not in by_name and "caching.memoize" not in by_name


def test_symbol_detail_from_signature_and_docstring() -> None:
    entry = get_symbol("retry")
    assert entry is not None
    assert entry.kind == "function" and entry.signature.startswith("retry(")
    params = {param.name: param for param in entry.params}
    assert params["max_attempts"].kind == "keyword_only"
    assert params["max_attempts"].annotation == "int | None"
    assert "Total number of calls" in params["max_attempts"].description
    assert params["func"].kind == "positional_only"
    assert entry.summary.startswith("Retry a function")
    assert " at 0x" not in entry.signature


def test_raises_and_example_are_extracted() -> None:
    explain = get_symbol("doctor.explain")
    assert explain is not None
    assert "from python_wrap.doctor import explain" in explain.example
    assert explain.returns.startswith("A `Report`")
    with_raises = [entry for entry in api_index() if entry.raises]
    assert with_raises, "some public docstring documents Raises:"
    exc, _ = with_raises[0].raises[0]
    assert exc and exc[0].isalpha()


@pytest.mark.parametrize(
    "spelling",
    ["doctor.explain", "python_wrap.doctor.explain", "explain", "  doctor.explain "],
)
def test_lookup_spellings(spelling: str) -> None:
    entry = get_symbol(spelling)
    assert entry is not None and entry.qualname == "python_wrap.doctor.explain"


def test_lookup_methods_and_unknown_names() -> None:
    reset = get_symbol("CircuitBreaker.reset")
    assert reset is not None and reset.qualname == "python_wrap.CircuitBreaker.reset"
    assert get_symbol("CircuitBreaker._private") is None
    assert get_symbol("no_such_thing") is None
    assert get_symbol("CircuitBreaker.no_such_method") is None


def test_render_is_readable() -> None:
    entry = get_symbol("hedge")
    assert entry is not None
    text = entry.render()
    assert text.startswith("python_wrap.hedge\n") and "Parameters:" in text


def test_to_dict_is_json_serialisable() -> None:
    for entry in api_index():
        json.dumps(entry.to_dict())


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("circuit breaker", "circuit_breaker"),
        ("fake clock", "testing.FakeClock"),
        ("singleflight", "singleflight"),
    ],
)
def test_search_ranks_names_first(query: str, expected: str) -> None:
    assert expected in [entry.name for entry in search(query, limit=3)]


def test_search_edge_cases() -> None:
    assert search("") == []
    assert search("the a of") == []  # stop words only
    assert search("zzzzqqqq") == []
    assert len(search("retry", limit=2)) == 2
    assert search("retry", limit=0) == []
    search("x" * 100_000)  # long input is truncated, not slow


@pytest.mark.parametrize(
    ("need", "api"),
    [
        ("retry http calls", "presets.http_client"),
        ("stop duplicate emails", "idempotent"),
        ("limit llm tokens", "Quota"),
        ("retrying requests that fail", "retry"),
        ("cache expensive database results", "memoize"),
        ("migrate from tenacity", "migrate.migrate_source"),
    ],
)
def test_recipes_map_needs_to_apis(need: str, api: str) -> None:
    found = find_recipes(need)
    assert found and api in found[0].apis, [r.need for r in found]
    assert found[0].score > 0


def test_unmatched_need_returns_nothing() -> None:
    assert find_recipes("zzzz qqqq") == []


def test_recipe_data_file_is_valid() -> None:
    raw = json.loads(
        resources.files("python_wrap").joinpath("_data").joinpath("recipes.json").read_text()
    )
    for item in raw["recipes"]:
        assert set(item) <= {"need", "keywords", "apis", "snippet", "notes", "docs"}, item
        assert item["need"] and item["keywords"] and item["apis"]
        for name in item["apis"]:
            assert get_symbol(name) is not None, f"{item['need']}: unknown API {name}"
        assert (ROOT / "docs" / item["docs"]).is_file(), item["docs"]
    assert len(recipes()) == len(raw["recipes"])


@pytest.mark.parametrize("recipe", recipes(), ids=lambda r: r.need)
def test_recipe_snippets_run(recipe: introspect.Recipe) -> None:
    if recipe.snippet:
        exec(compile(recipe.snippet, recipe.need, "exec"), {"__name__": "recipe_snippet"})


def test_recipe_rendering() -> None:
    recipe = find_recipes("rate limit an api")[0]
    data = recipe.to_dict()
    assert data["apis"][0]["qualname"].startswith("python_wrap.")
    assert "keywords" not in data
    assert "Docs: features/concurrency/rate-limits.md" in recipe.render()


def test_guide_is_packaged_and_covers_the_rules() -> None:
    text = guide_text()
    for heading in (
        "Layer order",
        "Idempotency rules",
        "What never to retry",
        "Common mistakes",
        "Check your work with the doctor",
    ):
        assert heading in text
    path = resources.files("python_wrap").joinpath("_data").joinpath("agent_guide.md")
    assert path.is_file()


def test_guide_layer_order_matches_policy_roles() -> None:
    from python_wrap.policy import ROLES

    assert " -> ".join(ROLES) in guide_text()


# --------------------------------------------------------------------------- CLI


def test_cli_api_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api"]) == 0
    out = capsys.readouterr().out
    assert "retry" in out and "doctor.explain" in out


def test_cli_api_symbol_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "memoize"]) == 0
    assert "python_wrap.memoize" in capsys.readouterr().out
    assert main(["api", "memoize", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "memoize" and data["params"]
    assert main(["api", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {"name", "qualname", "kind", "signature", "summary"} == set(listing[0])


def test_cli_api_unknown_symbol(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "retri"]) == 1
    assert "unknown symbol 'retri'" in capsys.readouterr().err


def test_cli_api_search(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "search", "rate", "limit"]) == 0
    assert "rate_limit" in capsys.readouterr().out
    assert main(["api", "search", "rate", "--json", "--limit", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["api", "search", "zzzzqqqq"]) == 1


def test_cli_recipe(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["recipe", "stop", "duplicate", "emails"]) == 0
    assert "python_wrap.idempotent" in capsys.readouterr().out
    assert main(["recipe", "limit llm tokens", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["apis"][0]["name"] == "Quota"
    assert main(["recipe"]) == 0
    assert "Retry flaky HTTP calls" in capsys.readouterr().out
    assert main(["recipe", "zzzz", "qqqq"]) == 1
    assert "no recipe matched" in capsys.readouterr().err


def test_cli_guide(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["guide"]) == 0
    assert "What never to retry" in capsys.readouterr().out


def test_cli_usage_lists_new_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for command in ("api", "recipe", "guide", "mcp"):
        assert command in out


def test_module_entry_point() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "python_wrap", "api", "retry", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=True,
    )
    assert json.loads(result.stdout)["qualname"] == "python_wrap.retry"
