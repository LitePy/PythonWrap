"""Generate ``docs/llms.txt`` and ``docs/llms-full.txt`` (https://llmstxt.org).

Both files are built from what already exists, so new pages and names appear on the
next run without editing this script:

* ``llms.txt``: one section per group of the ``mkdocs.yml`` nav, a link and a one-line
  description per page (the first paragraph of the page, or the module docstring for
  ``::: module`` API pages); the ``Project`` group goes under ``Optional``.
* ``llms-full.txt``: the packaged agent guide, the key pages in full (``Get started``,
  any ``AI & agents`` group and the pages in `EXTRA_FULL`), then every public name from
  `python_wrap.introspect.api_index` with its signature and docstring summary.

MkDocs copies both files to the site root, so they are served at ``<site>/llms.txt``.
``tests/test_llms_txt.py`` fails when the committed files are stale.

Usage:
    uv run python scripts/gen_llms.py           # rewrite the files
    uv run python scripts/gen_llms.py --check   # exit 1 if they are out of date
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
OUTPUTS = {"llms.txt": DOCS / "llms.txt", "llms-full.txt": DOCS / "llms-full.txt"}
sys.path.insert(0, str(ROOT / "src"))

#: Nav groups (top level) listed under "Optional" in llms.txt.
OPTIONAL_GROUPS = {"Project"}
#: Nav groups whose pages are copied in full into llms-full.txt, at any depth.
FULL_GROUPS = {"Get started", "AI & agents"}
#: Further pages copied in full.
EXTRA_FULL = (
    "features/resilience/policies.md",
    "features/resilience/presets-and-doctor.md",
    "testing/index.md",
)
_NAV_LINE = re.compile(
    r"^(?P<indent>\s*)-\s+(?:(?P<title>[^:]+?):\s*(?P<path>\S+)?|(?P<bare>\S+))\s*$"
)
_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*:")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


@dataclass
class Node:
    """One nav entry: a page (``path``) or a group (``children``)."""

    title: str
    path: str | None = None
    children: list[Node] = field(default_factory=list)


def _config_value(key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", MKDOCS.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_nav(text: str) -> list[Node]:
    """Parse the ``nav:`` block of mkdocs.yml (titles, paths and nesting only)."""
    lines = text.splitlines()
    start = lines.index("nav:") + 1
    root = Node("")
    stack: list[tuple[int, Node]] = [(-1, root)]
    for line in lines[start:]:
        if _TOP_KEY.match(line):
            break
        match = _NAV_LINE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if match.group("bare"):
            node = Node("", match.group("bare"))
        else:
            node = Node(match.group("title").strip().strip("\"'"), match.group("path"))
        parent.children.append(node)
        if node.path is None:
            stack.append((indent, node))
    return root.children


def page_url(site: str, path: str) -> str:
    """The served URL of a docs page (MkDocs directory URLs)."""
    slug = path[: -len("index.md")] if path.endswith("index.md") else path[: -len(".md")] + "/"
    return site.rstrip("/") + "/" + slug


def _clean(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"\*\*|__|<[^>]+>|\{[^}]*\}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_sentence(text: str, limit: int = 180) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 3].rstrip() + "..."


def describe(path: str) -> str:
    """One line: the module summary for ``::: module`` pages, else the first paragraph."""
    file = DOCS / path
    if not file.is_file():
        return ""
    text = file.read_text(encoding="utf-8")
    directive = re.search(r"^::: ([\w.]+)", text, re.MULTILINE)
    if directive:
        from python_wrap.introspect import get_symbol

        entry = get_symbol(directive.group(1))
        if entry is not None and entry.summary:
            return _first_sentence(entry.summary)
    in_front_matter = text.startswith("---")
    in_fence = False
    paragraph: list[str] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if in_front_matter:
            if index and stripped == "---":
                in_front_matter = False
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if stripped[0] in "#<!|:>-*=" or stripped.startswith(("::: ", "[")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return _first_sentence(_clean(" ".join(paragraph)))


def _title(node: Node) -> str:
    if node.title:
        return node.title
    file = DOCS / (node.path or "")
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return _clean(line[2:])
    return node.path or ""


def _pages(nodes: list[Node]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        if node.path:
            out.append(node)
        out += _pages(node.children)
    return out


def _link(site: str, node: Node, summary: str = "") -> str:
    assert node.path is not None
    description = summary if node.path == "index.md" and summary else describe(node.path)
    line = f"- [{_title(node)}]({page_url(site, node.path)})"
    return f"{line}: {description}" if description else line


def render_llms(nav: list[Node], site: str, summary: str) -> str:
    """The ``llms.txt`` index."""
    out = [
        "# python-wrap",
        "",
        f"> {summary}",
        "",
        "Install with `pip install python-wrap` (Python 3.10+, no runtime dependencies). "
        "Before writing code with it, read the agent guide (`python -m python_wrap guide`), "
        "find the right API with `python -m python_wrap recipe <need>` or "
        "`python -m python_wrap api search <words>`, check exact parameters with "
        "`python -m python_wrap api <symbol>`, and lint the result with "
        "`python -m python_wrap doctor module:attribute`. `python -m python_wrap mcp` serves "
        "the same tools over the Model Context Protocol. "
        f"Everything in one file: [llms-full.txt]({site.rstrip('/')}/llms-full.txt).",
    ]
    loose = [node for node in nav if node.path]
    groups = [node for node in nav if node.path is None]
    optional: list[str] = []
    if loose:
        out += ["", "## Docs", ""] + [_link(site, node, summary) for node in loose]
    for group in groups:
        lines = [_link(site, node) for node in group.children if node.path]
        sections = [(group.title, lines)]
        for child in group.children:
            if child.path is None:
                sections.append(
                    (f"{group.title}: {child.title}", [_link(site, n) for n in _pages([child])])
                )
        for title, links in sections:
            if not links:
                continue
            if group.title in OPTIONAL_GROUPS:
                optional += links
            else:
                out += ["", f"## {title}", "", *links]
    if optional:
        out += ["", "## Optional", "", *optional]
    return "\n".join(out) + "\n"


def _full_pages(nav: list[Node]) -> list[Node]:
    chosen: list[Node] = []

    def walk(nodes: list[Node], inside: bool) -> None:
        for node in nodes:
            selected = inside or node.title in FULL_GROUPS
            if node.path and selected:
                chosen.append(node)
            walk(node.children, selected)

    walk(nav, False)
    known = {node.path for node in chosen}
    by_path = {node.path: node for node in _pages(nav)}
    for path in EXTRA_FULL:
        if path not in known and path in by_path:
            chosen.append(by_path[path])
    return chosen


def _demote(text: str) -> str:
    """Turn ``#`` headings into ``##`` (outside code fences) to nest under our H1."""
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        out.append("#" + line if not in_fence and line.startswith("#") else line)
    return "\n".join(out)


def render_full(nav: list[Node], site: str, summary: str) -> str:
    """The ``llms-full.txt`` bundle: guide, key pages and the API index."""
    from python_wrap.introspect import api_index, guide_text

    out = [
        "# python-wrap: full reference for language models",
        "",
        f"> {summary}",
        "",
        "Generated by scripts/gen_llms.py from the documentation and docstrings.",
        "",
        "## Agent guide",
        "",
        guide_text().strip().removeprefix("# ").split("\n", 1)[-1].strip(),
    ]
    for node in _full_pages(nav):
        assert node.path is not None
        text = (DOCS / node.path).read_text(encoding="utf-8").strip()
        text = _demote(text)
        out += ["", "---", "", f"Source: {page_url(site, node.path)}", "", text]
    out += [
        "",
        "---",
        "",
        "## Public API",
        "",
        "Every public name: `python_wrap.__all__` first, then names public only in a "
        "submodule. `python -m python_wrap api <name>` prints parameters, raises and an "
        "example for each.",
    ]
    for entry in api_index():
        line = f"- `{entry.qualname}`"
        if entry.signature:
            line += f": `{entry.signature}`"
        out.append(line)
        if entry.summary:
            out.append(f"  {entry.summary}")
    return "\n".join(out) + "\n"


def render() -> dict[str, str]:
    """Return ``{filename: content}`` for both files."""
    nav = parse_nav(MKDOCS.read_text(encoding="utf-8"))
    site = _config_value("site_url") or "/"
    summary = _config_value("site_description")
    return {
        "llms.txt": render_llms(nav, site, summary),
        "llms-full.txt": render_full(nav, site, summary),
    }


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, content in render().items():
        path = OUTPUTS[name]
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            continue
        stale.append(name)
        if not check:
            path.write_text(content, encoding="utf-8", newline="\n")
    if check and stale:
        print(f"out of date: {', '.join(stale)}; run python scripts/gen_llms.py", file=sys.stderr)
        return 1
    print("updated: " + ", ".join(stale) if stale else "llms files are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
