"""docs/llms.txt and docs/llms-full.txt are generated and must be committed up to date."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

import python_wrap

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_llms", ROOT / "scripts" / "gen_llms.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_llms"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    rendered: dict[str, str] = _load_generator().render()
    return rendered


@pytest.mark.parametrize("name", ["llms.txt", "llms-full.txt"])
def test_committed_files_are_up_to_date(generated: dict[str, str], name: str) -> None:
    committed = (DOCS / name).read_text(encoding="utf-8")
    assert committed == generated[name], f"docs/{name} is stale: run python scripts/gen_llms.py"


def test_llms_txt_follows_the_llmstxt_format(generated: dict[str, str]) -> None:
    lines = generated["llms.txt"].splitlines()
    assert lines[0] == "# python-wrap"
    assert lines[2].startswith("> ")
    sections = [line[3:] for line in lines if line.startswith("## ")]
    assert sections[-1] == "Optional"
    assert "Get started" in sections
    links = [line for line in lines if line.startswith("- [")]
    assert len(links) > 50
    for line in links:
        assert re.match(r"^- \[[^\]]+\]\(https?://[^)]+/\)(: .+)?$", line), line
    assert sum(1 for line in links if ": " in line.split(")", 1)[1]) / len(links) > 0.9


def test_every_nav_page_is_listed(generated: dict[str, str]) -> None:
    gen = _load_generator()
    nav = gen.parse_nav((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    paths = [node.path for node in gen._pages(nav)]
    assert "features/ai/agent-friendly.md" in paths
    for path in paths:
        assert (
            f"({gen.page_url('https://pythonwrap.readthedocs.io/', path)})" in generated["llms.txt"]
        ), path


def test_every_public_name_is_in_llms_full(generated: dict[str, str]) -> None:
    full = generated["llms-full.txt"]
    for name in python_wrap.__all__:
        assert f"`python_wrap.{name}`" in full, name


def test_llms_full_contains_guide_and_key_pages(generated: dict[str, str]) -> None:
    full = generated["llms-full.txt"]
    assert "## Layer order (outermost first)" in full
    assert "Source: https://pythonwrap.readthedocs.io/get-started/quickstart/" in full
    assert "Source: https://pythonwrap.readthedocs.io/features/ai/agent-friendly/" in full
    assert " at 0x" not in full  # no memory addresses: output is reproducible


def test_page_urls_follow_mkdocs_directory_urls() -> None:
    gen = _load_generator()
    assert gen.page_url("https://x.test/", "index.md") == "https://x.test/"
    assert gen.page_url("https://x.test", "a/index.md") == "https://x.test/a/"
    assert gen.page_url("https://x.test/", "a/b.md") == "https://x.test/a/b/"


def test_nav_parser_handles_groups_and_bare_paths() -> None:
    gen = _load_generator()
    nav = gen.parse_nav(
        "site_name: x\nnav:\n  - Home: index.md\n  - Group:\n      - group/index.md\n"
        "      - Sub:\n          - Page: group/sub/page.md\n  - Other: other.md\nplugins: []\n"
    )
    assert [node.title for node in nav] == ["Home", "Group", "Other"]
    group = nav[1]
    assert group.path is None and group.children[0].path == "group/index.md"
    assert group.children[1].children[0].path == "group/sub/page.md"
