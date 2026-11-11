"""A machine-readable index of the public API, plain-language recipes and the agent guide.

Everything here is generated at run time from ``__all__``, docstrings and signatures, so
new modules and names show up without editing this file. It backs the commands

* ``python -m python_wrap api [SYMBOL | search WORDS...] [--json]``
* ``python -m python_wrap recipe [NEED...] [--json]``
* ``python -m python_wrap guide``

and the read-only tools of `python_wrap.mcp`. Nothing is called: only ``__all__``,
docstrings and signatures are read. Importing a python-wrap submodule never imports an
optional dependency, so building the index needs only the standard library.

The recipe table lives in ``python_wrap/_data/recipes.json``; add an entry there to teach
``recipe`` (and the MCP ``recipe`` tool) a new need.

Example:
    ```python
    from python_wrap.introspect import find_recipes, get_symbol, search

    entry = get_symbol("retry")
    assert entry is not None and entry.name == "retry"
    assert "max_attempts" in [param.name for param in entry.params]
    assert search("circuit breaker")[0].name in {"circuit_breaker", "CircuitBreaker"}
    assert find_recipes("stop duplicate emails")[0].apis
    ```
"""

from __future__ import annotations

import argparse
import enum
import functools
import importlib
import inspect
import json
import pkgutil
import re
import sys
import textwrap
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any

__all__ = [
    "Param",
    "Recipe",
    "Symbol",
    "api_index",
    "find_recipes",
    "get_symbol",
    "guide_text",
    "recipes",
    "search",
]

_PACKAGE = "python_wrap"
_SKIP_MODULES = {"__main__"}
_SECTION = re.compile(r"^(?P<name>[A-Z][A-Za-z ]*):\s*$")
_ITEM = re.compile(r"^(?P<name>\*{0,2}[A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*(?P<desc>.*)$")
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
_FORWARD_REF = re.compile(r"ForwardRef\('([^']*)'(?:, [^)]*)?\)")
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "make",
        "me",
        "my",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "their",
        "them",
        "this",
        "to",
        "too",
        "up",
        "use",
        "want",
        "we",
        "when",
        "with",
        "without",
        "you",
        "your",
    ]
)
_MAX_QUERY = 500


@dataclass(frozen=True)
class Param:
    """One parameter of a callable.

    Attributes:
        name: Parameter name (``*args`` and ``**kwargs`` keep their stars).
        kind: ``positional_only``, ``positional_or_keyword``, ``var_positional``,
            ``keyword_only`` or ``var_keyword``.
        annotation: The annotation as written, or ``""``.
        default: A stable rendering of the default, or ``None`` when there is none.
        description: The text from the docstring's ``Args:`` section, or ``""``.
    """

    name: str
    kind: str
    annotation: str = ""
    default: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Symbol:
    """One public name.

    Attributes:
        name: Short name used for lookups: ``"retry"`` for top-level names,
            ``"doctor.explain"`` for names public only in a submodule.
        qualname: Full import path, such as ``"python_wrap.doctor.explain"``.
        kind: ``class``, ``function``, ``module``, ``alias`` (a type alias) or
            ``constant``.
        signature: ``name(params) -> return`` for callables, ``""`` otherwise.
        summary: First line of the docstring.
        params: Parameters merged with their ``Args:`` descriptions.
        returns: The docstring's ``Returns:`` section.
        raises: ``(exception, description)`` pairs from ``Raises:``.
        example: A minimal example: the first python code block of the docstring.
        doc: The full docstring (dedented).
    """

    name: str
    qualname: str
    kind: str
    signature: str
    summary: str
    params: tuple[Param, ...] = ()
    returns: str = ""
    raises: tuple[tuple[str, str], ...] = ()
    example: str = ""
    doc: str = ""

    def to_dict(self, *, full: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable dict; ``full=False`` keeps only the headline fields."""
        if not full:
            return {
                "name": self.name,
                "qualname": self.qualname,
                "kind": self.kind,
                "signature": self.signature,
                "summary": self.summary,
            }
        data = asdict(self)
        data["raises"] = [{"exception": exc, "description": desc} for exc, desc in self.raises]
        return data

    def render(self) -> str:
        """Human-readable detail: signature, summary, parameters, raises and example."""
        lines = [self.qualname, "", self.signature or self.kind, "", self.summary]
        if self.params:
            lines += ["", "Parameters:"]
            for param in self.params:
                head = f"  {param.name}"
                if param.annotation:
                    head += f": {param.annotation}"
                if param.default is not None:
                    head += f" = {param.default}"
                lines.append(head)
                if param.description:
                    lines.append(textwrap.indent(param.description, "      "))
        if self.returns:
            lines += ["", "Returns:", textwrap.indent(self.returns, "  ")]
        if self.raises:
            lines += ["", "Raises:"]
            lines += [f"  {exc}: {desc}" if desc else f"  {exc}" for exc, desc in self.raises]
        if self.example:
            lines += ["", "Example:", textwrap.indent(self.example, "  ")]
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class Recipe:
    """A plain-language need mapped to the APIs that solve it.

    Attributes:
        need: The need, phrased the way someone would ask for it.
        apis: Index names to use (see `Symbol.name`); names that do not exist in this
            installation are dropped.
        snippet: A minimal, runnable example.
        notes: Pitfalls and what not to do.
        docs: Path of the docs page, relative to the docs root.
        keywords: Extra words that should match this recipe.
        score: How well it matched the query (0 when listed without a query).
    """

    need: str
    apis: tuple[str, ...]
    snippet: str = ""
    notes: str = ""
    docs: str = ""
    keywords: tuple[str, ...] = field(default=(), repr=False)
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict, with a one-line summary of each API."""
        data = asdict(self)
        data["apis"] = [_api_brief(name) for name in self.apis]
        data.pop("keywords")
        return data

    def render(self) -> str:
        """Human-readable recipe."""
        lines = [self.need, ""]
        for name in self.apis:
            brief = _api_brief(name)
            lines.append(f"  {brief['qualname']}: {brief['summary']}")
        if self.snippet:
            lines += ["", textwrap.indent(self.snippet.rstrip(), "    ")]
        if self.notes:
            lines += ["", textwrap.fill(self.notes, 88, initial_indent="Note: ")]
        if self.docs:
            lines += ["", f"Docs: {self.docs}"]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- rendering


def _render_value(value: Any) -> str:
    """A stable repr: no memory addresses, sorted sets, classes and functions by name."""
    if isinstance(value, (set, frozenset)):
        items = ", ".join(sorted(_render_value(item) for item in value))
        if not value:
            return f"{type(value).__name__}()"
        return f"{{{items}}}" if type(value) is set else f"frozenset({{{items}}})"
    if isinstance(value, tuple):
        inner = ", ".join(_render_value(item) for item in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = (f"{_render_value(k)}: {_render_value(v)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if inspect.isclass(value) or inspect.isroutine(value):
        return str(getattr(value, "__qualname__", getattr(value, "__name__", repr(value))))
    if type(value) is object:
        return "<unset>"
    text = repr(value)
    return _ADDRESS.sub("", text) if len(text) <= 200 else f"<{type(value).__name__}>"


def _render_annotation(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    text = _FORWARD_REF.sub(r"\1", inspect.formatannotation(annotation))
    return _ADDRESS.sub("", text)


_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "positional_only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional_or_keyword",
    inspect.Parameter.VAR_POSITIONAL: "var_positional",
    inspect.Parameter.KEYWORD_ONLY: "keyword_only",
    inspect.Parameter.VAR_KEYWORD: "var_keyword",
}


def _signature(obj: Any) -> inspect.Signature | None:
    if not callable(obj):
        return None
    try:
        return inspect.signature(obj)
    except (TypeError, ValueError):
        return None


_STARS: dict[Any, str] = {
    inspect.Parameter.VAR_POSITIONAL: "*",
    inspect.Parameter.VAR_KEYWORD: "**",
}


def _format_signature(name: str, sig: inspect.Signature) -> str:
    parts: list[str] = []
    seen_kw_only = False
    params = list(sig.parameters.values())
    for index, param in enumerate(params):
        if param.kind is inspect.Parameter.KEYWORD_ONLY and not seen_kw_only:
            if not any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
                parts.append("*")
            seen_kw_only = True
        prefix = _STARS.get(param.kind, "")
        text = prefix + param.name
        annotation = _render_annotation(param.annotation)
        if annotation:
            text += f": {annotation}"
        if param.default is not inspect.Parameter.empty:
            default = _render_value(param.default)
            text += f" = {default}" if annotation else f"={default}"
        parts.append(text)
        is_last_positional_only = param.kind is inspect.Parameter.POSITIONAL_ONLY and (
            index + 1 == len(params) or params[index + 1].kind is not param.kind
        )
        if is_last_positional_only:
            parts.append("/")
    out = f"{name}({', '.join(parts)})"
    returns = _render_annotation(sig.return_annotation)
    return f"{out} -> {returns}" if returns else out


# --------------------------------------------------------------------------- docstrings


def _sections(doc: str) -> dict[str, str]:
    """Split a Google-style docstring into ``{"": body, "Args": ..., ...}``."""
    out: dict[str, list[str]] = {"": []}
    current = ""
    for line in doc.splitlines():
        match = _SECTION.match(line)
        if match and not line.startswith(" "):
            current = match.group("name")
            out.setdefault(current, [])
            continue
        out[current].append(line)
    return {key: textwrap.dedent("\n".join(value)).strip("\n") for key, value in out.items()}


def _items(section: str) -> list[tuple[str, str]]:
    """Parse ``name: description`` items with indented continuation lines."""
    items: list[tuple[str, list[str]]] = []
    for line in section.splitlines():
        match = _ITEM.match(line) if line and not line[0].isspace() else None
        if match:
            items.append((match.group("name").lstrip("*"), [match.group("desc").strip()]))
        elif items and line.strip():
            items[-1][1].append(line.strip())
    return [(name, " ".join(part for part in desc if part)) for name, desc in items]


def _example(sections: Mapping[str, str], doc: str) -> str:
    for key in ("Example", "Examples"):
        text = sections.get(key, "")
        if text:
            fenced = _first_fence(text)
            return fenced if fenced is not None else text
    fenced = _first_fence(doc)
    return fenced or ""


def _first_fence(text: str) -> str | None:
    match = re.search(r"```(?:python|py|pycon)?[^\n]*\n(.*?)\n\s*```", text, re.DOTALL)
    return textwrap.dedent(match.group(1)).strip("\n") if match else None


def _kind(obj: Any) -> str:
    if inspect.ismodule(obj):
        return "module"
    if isinstance(obj, types.GenericAlias) or type(obj).__module__ in {"typing", "types"}:
        return "alias"  # before isclass: ``tuple[int, float]`` is a class on 3.10
    if inspect.isclass(obj):
        return "class"
    if callable(obj):
        return "function"
    return "constant"


def _describe(name: str, qualname: str, obj: Any) -> Symbol:
    kind = _kind(obj)
    raw_doc = inspect.getdoc(obj) if kind not in {"constant", "alias"} else None
    doc = raw_doc or ""
    sections = _sections(doc)
    summary = sections[""].strip().split("\n\n", 1)[0].replace("\n", " ") if doc else ""
    if kind == "constant":
        summary = summary or f"Constant: {_render_value(obj)}"
    if kind == "alias":
        summary = f"Type alias: {_render_annotation(obj)}"
    sig = _signature(obj) if kind in {"class", "function"} else None
    if kind == "class" and (
        issubclass(obj, enum.Enum) or getattr(obj, "_is_protocol", False)
    ):  # their call signatures are machinery that differs between Python versions
        sig = None
    described = dict(_items(sections.get("Args", sections.get("Arguments", ""))))
    params: tuple[Param, ...] = ()
    signature = ""
    if sig is not None:
        if kind == "class":  # a class "returns" an instance; versions disagree on how
            sig = sig.replace(return_annotation=inspect.Signature.empty)
        signature = _format_signature(name.rsplit(".", 1)[-1], sig)
        params = tuple(
            Param(
                name=param.name,
                kind=_KINDS[param.kind],
                annotation=_render_annotation(param.annotation),
                default=None
                if param.default is inspect.Parameter.empty
                else _render_value(param.default),
                description=described.get(param.name, ""),
            )
            for param in sig.parameters.values()
        )
    return Symbol(
        name=name,
        qualname=qualname,
        kind=kind,
        signature=signature,
        summary=summary,
        params=params,
        returns=sections.get("Returns", ""),
        raises=tuple(_items(sections.get("Raises", ""))),
        example=_example(sections, doc),
        doc=doc,
    )


# --------------------------------------------------------------------------- the index


def _public_modules() -> list[str]:
    package = importlib.import_module(_PACKAGE)
    names = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _SKIP_MODULES:
            continue
        names.append(info.name)
    return sorted(names)


@functools.lru_cache(maxsize=1)
def _index() -> tuple[Symbol, ...]:
    package = importlib.import_module(_PACKAGE)
    entries: list[Symbol] = []
    seen: set[int] = set()
    top_level = set(getattr(package, "__all__", ()))
    for name in sorted(top_level):
        obj = getattr(package, name)
        if not isinstance(obj, (str, int, float, bool)):
            seen.add(id(obj))
        entries.append(_describe(name, f"{_PACKAGE}.{name}", obj))
    for module_name in _public_modules():
        try:
            module = importlib.import_module(f"{_PACKAGE}.{module_name}")
        except Exception:  # an optional extra that cannot import here: skip it
            continue
        if module_name not in top_level:  # e.g. the ``retry`` module vs ``retry()``
            entries.append(_describe(module_name, f"{_PACKAGE}.{module_name}", module))
        for name in sorted(getattr(module, "__all__", ())):
            obj = getattr(module, name, None)
            if obj is None or id(obj) in seen or inspect.ismodule(obj):
                continue
            seen.add(id(obj))
            entries.append(_describe(f"{module_name}.{name}", f"{module.__name__}.{name}", obj))
    return tuple(entries)


def api_index() -> tuple[Symbol, ...]:
    """Every public name: the package ``__all__`` first, then submodule-only names.

    Submodules are found with `pkgutil`; each contributes its own ``__all__`` (names
    already exported at the top level are not repeated) and an entry for the module.

    Returns:
        A tuple of `Symbol`, computed once per process.
    """
    return _index()


def get_symbol(name: str) -> Symbol | None:
    """Look up a public name.

    Args:
        name: ``"retry"``, ``"doctor.explain"``, ``"python_wrap.doctor.explain"``, a
            short name that is unique among submodule names (``"explain"``), or
            ``Class.attribute`` (``"CircuitBreaker.reset"``).

    Returns:
        The `Symbol`, or ``None`` when nothing matches.
    """
    name = name.strip()
    if name.startswith(f"{_PACKAGE}."):
        name = name[len(_PACKAGE) + 1 :]
    by_name = {entry.name: entry for entry in api_index()}
    if name in by_name:
        return by_name[name]
    tails = [entry for entry in api_index() if entry.name.rsplit(".", 1)[-1] == name]
    if len(tails) == 1:
        return tails[0]
    head, _, attr = name.rpartition(".")
    parent = by_name.get(head)
    if parent is not None and attr and not attr.startswith("_"):
        module_name, _, obj_path = parent.qualname.rpartition(".")
        obj: Any = importlib.import_module(module_name)
        for part in [*obj_path.split("."), attr]:
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return _describe(name, f"{parent.qualname}.{attr}", obj)
    return None


def _tokens(text: str) -> list[str]:
    words = _WORD.findall(text[:_MAX_QUERY].lower().replace("_", " "))
    return [_stem(word) for word in words if word not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def search(query: str, *, limit: int = 10) -> list[Symbol]:
    """Rank public names by how well their name and docstring match ``query``.

    Words in the name weigh most, then the summary, then the rest of the docstring.

    Args:
        query: Free text, such as ``"rate limit per user"``.
        limit: Maximum number of results.

    Returns:
        Matching symbols, best first; empty when no word matches.
    """
    words = set(_tokens(query))
    if not words:
        return []
    scored: list[tuple[int, str, Symbol]] = []
    for entry in api_index():
        name_words = set(_tokens(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", entry.name)))
        summary_words = set(_tokens(entry.summary))
        doc_words = set(_tokens(entry.doc))
        name_hits = len(words & name_words)
        top_level = "." not in entry.name and entry.kind != "module"
        score = 6 * name_hits + 3 * len(words & summary_words) + len(words & doc_words)
        if score and name_hits and top_level:
            score += 2
        if name_words and name_words <= words:  # every word of the name was asked for
            score += 4
        if score:
            scored.append((-score, entry.name, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[: max(limit, 0)]]


# --------------------------------------------------------------------------- recipes


def _data(name: str) -> str:
    return resources.files(_PACKAGE).joinpath("_data").joinpath(name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _recipes() -> tuple[Recipe, ...]:
    raw = json.loads(_data("recipes.json"))
    known = {entry.name for entry in api_index()}
    out = []
    for item in raw["recipes"]:
        apis = tuple(name for name in item["apis"] if name in known or get_symbol(name))
        out.append(
            Recipe(
                need=item["need"],
                apis=apis,
                snippet=item.get("snippet", ""),
                notes=item.get("notes", ""),
                docs=item.get("docs", ""),
                keywords=tuple(item.get("keywords", ())),
            )
        )
    return tuple(out)


def recipes() -> tuple[Recipe, ...]:
    """Every recipe in ``python_wrap/_data/recipes.json``, in file order."""
    return _recipes()


def find_recipes(need: str, *, limit: int = 3) -> list[Recipe]:
    """Map a plain-language need to recipes, best first.

    A recipe matches on the words of its ``need`` and its ``keywords``; plurals and
    ``-ing``/``-ed`` forms are folded, so "retrying requests" matches "retry request".

    Args:
        need: Free text, such as ``"stop duplicate emails"``.
        limit: Maximum number of recipes.

    Returns:
        Matching recipes with their `Recipe.score`, best first; empty when nothing
        matches (use `search` then).
    """
    words = set(_tokens(need))
    scored = []
    for position, recipe in enumerate(recipes()):
        keywords = set(_tokens(" ".join(recipe.keywords)))
        need_words = set(_tokens(recipe.need))
        score = 2 * len(words & keywords) + len(words & need_words)
        if score:
            scored.append((-score, position, recipe))
    scored.sort(key=lambda item: (item[0], item[1]))
    best = -scored[0][0] if scored else 0
    return [  # weak matches far behind the best one are noise
        Recipe(r.need, r.apis, r.snippet, r.notes, r.docs, r.keywords, -s)
        for s, _, r in scored[: max(limit, 0)]
        if -s * 2 >= best
    ]


def _api_brief(name: str) -> dict[str, str]:
    entry = get_symbol(name)
    if entry is None:
        return {"name": name, "qualname": name, "summary": ""}
    return {"name": entry.name, "qualname": entry.qualname, "summary": entry.summary}


def guide_text() -> str:
    """The agent guide shipped in the wheel (``python_wrap/_data/agent_guide.md``)."""
    return _data("agent_guide.md")


# --------------------------------------------------------------------------- CLI


def _emit(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def _symbol_list(entries: Iterable[Symbol]) -> str:
    rows = [(entry.name, entry.summary) for entry in entries]
    width = min(max((len(name) for name, _ in rows), default=0), 32)
    return "\n".join(f"{name:<{width}}  {summary}".rstrip() for name, summary in rows)


def api_main(argv: Sequence[str] | None = None) -> int:
    """``python -m python_wrap api``: list, describe or search public names."""
    parser = argparse.ArgumentParser(
        prog="python -m python_wrap api",
        description="List public names, describe one, or search them: "
        "'api', 'api SYMBOL', 'api search WORDS...'.",
    )
    parser.add_argument("words", nargs="*", help="a symbol, or 'search' followed by words")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--limit", type=int, default=10, help="maximum search results")
    args = parser.parse_args(argv)
    words: list[str] = args.words
    if not words:
        entries = api_index()
        if args.json:
            _emit([entry.to_dict(full=False) for entry in entries])
        else:
            print(_symbol_list(entries))
        return 0
    if words[0] == "search":
        found = search(" ".join(words[1:]), limit=args.limit)
        if args.json:
            _emit([entry.to_dict(full=False) for entry in found])
        elif found:
            print(_symbol_list(found))
        else:
            print("no match", file=sys.stderr)
        return 0 if found else 1
    status = 0
    for name in words:
        entry = get_symbol(name)
        if entry is None:
            hints = ", ".join(item.name for item in search(name, limit=5))
            print(
                f"unknown symbol {name!r}" + (f"; try: {hints}" if hints else ""), file=sys.stderr
            )
            status = 1
        elif args.json:
            _emit(entry.to_dict())
        else:
            print(entry.render())
    return status


def recipe_main(argv: Sequence[str] | None = None) -> int:
    """``python -m python_wrap recipe NEED...``: which API solves a plain-language need."""
    parser = argparse.ArgumentParser(
        prog="python -m python_wrap recipe",
        description="Map a need ('retry http calls', 'stop duplicate emails') to the API.",
    )
    parser.add_argument("need", nargs="*", help="what you want to do; omit to list recipes")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--limit", type=int, default=3, help="maximum recipes")
    args = parser.parse_args(argv)
    if not args.need:
        listed = list(recipes())
        if args.json:
            _emit([item.to_dict() for item in listed])
        else:
            print("\n".join(item.need for item in listed))
        return 0
    found = find_recipes(" ".join(args.need), limit=args.limit)
    if args.json:
        _emit([item.to_dict() for item in found])
    elif found:
        print("\n".join(item.render() for item in found).rstrip())
    else:
        hints = ", ".join(item.name for item in search(" ".join(args.need), limit=5))
        print("no recipe matched" + (f"; related APIs: {hints}" if hints else ""), file=sys.stderr)
    return 0 if found else 1


def guide_main(argv: Sequence[str] | None = None) -> int:
    """``python -m python_wrap guide``: print the agent guide."""
    argparse.ArgumentParser(
        prog="python -m python_wrap guide",
        description="Print a concise guide to using python-wrap correctly.",
    ).parse_args(argv)
    print(guide_text().rstrip())
    return 0
