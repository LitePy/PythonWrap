"""Codemod that rewrites tenacity and backoff decorators to ``python_wrap.retry``.

Run it as ``python -m python_wrap migrate [--write] [--diff] PATH...``. By default it only
reports what it would change. It understands the common decorator shapes (``stop_after_*``,
``wait_fixed`` / ``wait_exponential`` / ``wait_random`` / ``wait_exponential_jitter``,
``retry_if_*``, ``backoff.on_exception`` / ``backoff.on_predicate``) and leaves anything else
untouched, reporting it with a line and column instead. The output is always checked with
`ast.parse`; if a rewrite would produce invalid Python the file is left as it was.

The module uses only the standard library and does not import tenacity or backoff.

Example:
    >>> from python_wrap.migrate import migrate_source
    >>> result = migrate_source(
    ...     "from tenacity import retry, stop_after_attempt, wait_fixed\\n"
    ...     "@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))\\n"
    ...     "def f(): ...\\n"
    ... )
    >>> print(result.text)
    from python_wrap import retry
    @retry(max_attempts=3, delay=2, backoff=1)
    def f(): ...
    <BLANKLINE>
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
import tokenize
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Change", "MigrationResult", "Unsupported", "main", "migrate_source"]

_SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".tox", ".mypy_cache", "node_modules"}
_LINE_LIMIT = 100


@dataclass(frozen=True)
class Change:
    """One decorator that was rewritten.

    Attributes:
        line: 1-based line of the decorator.
        col: 0-based column of the decorator expression.
        before: The original decorator source.
        after: The replacement decorator source.
        notes: Behaviour differences the author should review.
    """

    line: int
    col: int
    before: str
    after: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unsupported:
    """A construct the tool did not rewrite.

    Attributes:
        line: 1-based line.
        col: 0-based column.
        message: What was not understood and what to do about it.
    """

    line: int
    col: int
    message: str


@dataclass
class MigrationResult:
    """Outcome of `migrate_source`.

    Attributes:
        text: The new source (identical to the input when nothing changed).
        changes: Rewritten decorators, in source order.
        unsupported: Findings left for manual migration, in source order.
    """

    text: str
    changes: list[Change] = field(default_factory=list)
    unsupported: list[Unsupported] = field(default_factory=list)


class _Skip(Exception):
    """Raised while translating one decorator that cannot be migrated."""


# ---------------------------------------------------------------------------- source helpers


def _split_lines(text: str) -> list[str]:
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", text)


class _Source:
    """Maps ``ast`` (line, utf-8 byte column) positions to string offsets."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = _split_lines(text)
        self.starts: list[int] = []
        pos = 0
        for line in self.lines:
            self.starts.append(pos)
            pos += len(line)

    def offset(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))

    def span(self, node: ast.AST) -> tuple[int, int]:
        lineno, col = node.lineno, node.col_offset  # type: ignore[attr-defined]
        end_lineno, end_col = node.end_lineno, node.end_col_offset  # type: ignore[attr-defined]
        return self.offset(lineno, col), self.offset(end_lineno, end_col)

    def segment(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.text[start:end]

    def indent(self, lineno: int) -> str:
        line = self.lines[lineno - 1]
        return line[: len(line) - len(line.lstrip(" \t"))]


# ---------------------------------------------------------------------------- name resolution


class _Names:
    """Resolve names/attributes to canonical ``tenacity.x`` / ``backoff.x`` strings."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases: dict[str, str] = {}
        self.imports: list[ast.stmt] = []
        self.star: list[ast.ImportFrom] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                hit = False
                for alias in stmt.names:
                    root = alias.name.split(".")[0]
                    if root in ("tenacity", "backoff"):
                        hit = True
                        if alias.asname:
                            self.aliases[alias.asname] = alias.name
                        else:
                            self.aliases[root] = root
                if hit:
                    self.imports.append(stmt)
            elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
                if stmt.module.split(".")[0] in ("tenacity", "backoff"):
                    self.imports.append(stmt)
                    for alias in stmt.names:
                        if alias.name == "*":
                            self.star.append(stmt)
                        else:
                            self.aliases[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"

    def resolve(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def leaf(self, node: ast.expr) -> str | None:
        """The last component (``stop_after_attempt``) of a resolved name, or None."""
        full = self.resolve(node)
        return None if full is None else full.rsplit(".", 1)[-1]

    def library(self, node: ast.expr) -> str | None:
        full = self.resolve(node)
        return None if full is None else full.split(".")[0]


# ---------------------------------------------------------------------------- value helpers


def _number(node: ast.expr | None) -> int | float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return None if isinstance(node.value, bool) else node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value
    return None


def _fmt(value: int | float) -> str:
    if isinstance(value, float):
        value = round(value, 10)
        if value == int(value) and abs(value) < 1e15:
            return repr(float(value))
    return repr(value)


def _bind(call: ast.Call, params: Sequence[str]) -> dict[str, ast.expr]:
    """Bind positional/keyword args to ``params``; raise _Skip on anything odd."""
    bound: dict[str, ast.expr] = {}
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred) or i >= len(params):
            raise _Skip("unsupported positional arguments")
        bound[params[i]] = arg
    for kw in call.keywords:
        if kw.arg is None or kw.arg not in params:
            raise _Skip(f"unsupported argument {kw.arg or '**kwargs'!r}")
        if kw.arg in bound:
            raise _Skip(f"argument {kw.arg!r} given twice")
        bound[kw.arg] = kw.value
    return bound


def _flatten(node: ast.expr, op: type[ast.operator]) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, op):
        return _flatten(node.left, op) + _flatten(node.right, op)
    return [node]


# ---------------------------------------------------------------------------- translation


class _Settings:
    """Keyword arguments for the new ``retry(...)`` plus notes."""

    ORDER = (
        "on",
        "max_attempts",
        "delay",
        "backoff",
        "max_delay",
        "jitter",
        "max_time",
        "retry_if",
        "retry_if_result",
    )

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.notes: list[str] = []

    def set(self, key: str, value: str) -> None:
        if key in self.values:
            raise _Skip(f"{key} would be set twice")
        self.values[key] = value

    def render(self, name: str, indent: str, col: int) -> str:
        items = [f"{k}={self.values[k]}" for k in self.ORDER if k in self.values]
        one = f"{name}({', '.join(items)})"
        if col + len(one) + 1 <= _LINE_LIMIT and "\n" not in one:
            return one
        inner = indent + "    "
        body = "".join(f"{inner}{item},\n" for item in items)
        return f"{name}(\n{body}{indent})"


class _Translator:
    def __init__(self, src: _Source, names: _Names) -> None:
        self.src = src
        self.names = names

    def text(self, node: ast.expr) -> str:
        seg = self.src.segment(node)
        try:
            ast.parse(seg, mode="eval")
            return seg
        except SyntaxError:
            return ast.unparse(node)

    def call_of(self, node: ast.expr, library: str) -> tuple[str, ast.Call] | None:
        if isinstance(node, ast.Call) and self.names.library(node.func) == library:
            leaf = self.names.leaf(node.func)
            if leaf:
                return leaf, node
        return None

    # -- tenacity ------------------------------------------------------------------------

    def tenacity(self, deco: ast.expr) -> _Settings:
        out = _Settings()
        args: dict[str, ast.expr] = {}
        if isinstance(deco, ast.Call):
            if deco.args:
                raise _Skip("positional arguments to tenacity.retry are not supported")
            args = _bind(
                deco,
                (
                    "stop",
                    "wait",
                    "retry",
                    "reraise",
                    "before_sleep",
                    "before",
                    "after",
                    "sleep",
                    "retry_error_cls",
                    "retry_error_callback",
                    "statistics",
                ),
            )
        for key in ("before_sleep", "before", "after"):
            if key in args:
                raise _Skip(
                    f"{key}= hooks receive tenacity's RetryState; port the callback to "
                    "on_retry=(python_wrap.RetryState) or logger=/log_level= by hand"
                )
        for key in ("sleep", "retry_error_cls", "retry_error_callback", "statistics"):
            if key in args:
                raise _Skip(f"{key}= has no direct python_wrap equivalent")
        self._stop(args.get("stop"), out)
        self._wait(args.get("wait"), out)
        self._retry_cond(args.get("retry"), out)
        reraise = args.get("reraise")
        if not (isinstance(reraise, ast.Constant) and reraise.value is True):
            out.notes.append(
                "tenacity raised RetryError when attempts ran out; python_wrap re-raises the "
                "original exception, so update any `except RetryError` handlers"
            )
        return out

    def _stop(self, node: ast.expr | None, out: _Settings) -> None:
        if node is None:
            out.set("max_attempts", "None")
            out.notes.append("tenacity retries forever by default; max_attempts=None keeps that")
            return
        parts = _flatten(node, ast.BitOr)
        seen: set[str] = set()
        for part in parts:
            found = self.call_of(part, "tenacity") or (
                ("stop_never", None) if self._is_name(part, "stop_never") else None
            )
            if found is None:
                raise _Skip("stop expression not understood")
            leaf, call = found
            if leaf in seen:
                raise _Skip("repeated stop condition")
            seen.add(leaf)
            if leaf == "stop_never":
                out.set("max_attempts", "None")
            elif leaf == "stop_after_attempt" and call is not None:
                bound = _bind(call, ("max_attempt_number",))
                if "max_attempt_number" not in bound:
                    raise _Skip("stop_after_attempt needs an argument")
                out.set("max_attempts", self.text(bound["max_attempt_number"]))
            elif leaf == "stop_after_delay" and call is not None:
                bound = _bind(call, ("max_delay",))
                if "max_delay" not in bound:
                    raise _Skip("stop_after_delay needs an argument")
                out.set("max_time", self.text(bound["max_delay"]))
                out.notes.append(
                    "stop_after_delay becomes max_time: python_wrap stops when the next wait "
                    "would pass the budget instead of after the budget has passed"
                )
            else:
                raise _Skip(f"stop condition {leaf} is not supported")
        if "max_attempts" not in out.values:
            out.set("max_attempts", "None")

    def _is_name(self, node: ast.expr, leaf: str) -> bool:
        return self.names.library(node) == "tenacity" and self.names.leaf(node) == leaf

    def _wait(self, node: ast.expr | None, out: _Settings) -> None:
        if node is None:
            out.set("delay", "0")
            out.set("backoff", "1")
            return
        base: tuple[str, dict[str, ast.expr]] | None = None
        rand: dict[str, ast.expr] | None = None
        for part in _flatten(node, ast.Add):
            found = self.call_of(part, "tenacity")
            if found is None:
                raise _Skip("wait expression not understood")
            leaf, call = found
            if leaf == "wait_random":
                if rand is not None:
                    raise _Skip("two wait_random terms cannot be combined")
                rand = _bind(call, ("min", "max"))
            elif leaf in ("wait_fixed", "wait_none", "wait_exponential", "wait_exponential_jitter"):
                if base is not None:
                    raise _Skip("several base wait strategies cannot be combined")
                params = {
                    "wait_fixed": ("wait",),
                    "wait_none": (),
                    "wait_exponential": ("multiplier", "max", "exp_base", "min"),
                    "wait_exponential_jitter": ("initial", "max", "exp_base", "jitter"),
                }[leaf]
                base = (leaf, _bind(call, params))
            else:
                raise _Skip(f"wait strategy {leaf} is not supported")
        assert base is not None or rand is not None
        jitter_from_random: str | None = None
        rand_min: ast.expr | None = None
        if rand is not None:
            lo, hi = rand.get("min"), rand.get("max")
            lo_n = 0 if lo is None else _number(lo)
            hi_n = 1 if hi is None else _number(hi)
            if lo_n is not None and hi_n is not None:
                jitter_from_random = _fmt(hi_n - lo_n)
            else:
                hi_t = "1" if hi is None else self.text(hi)
                lo_t = "0" if lo is None else self.text(lo)
                jitter_from_random = f"({hi_t}) - ({lo_t})"
            rand_min = lo
        if base is None:
            assert rand is not None
            out.set("delay", "0" if rand_min is None else self.text(rand_min))
            out.set("backoff", "1")
            out.set("jitter", jitter_from_random or "0")
            return
        leaf, b = base
        if leaf == "wait_none":
            out.set("delay", "0")
            out.set("backoff", "1")
        elif leaf == "wait_fixed":
            if "wait" not in b:
                raise _Skip("wait_fixed needs an argument")
            out.set("delay", self.text(b["wait"]))
            out.set("backoff", "1")
        elif leaf == "wait_exponential":
            mult = b.get("multiplier")
            mult_n = 1 if mult is None else _number(mult)
            min_n = 0 if b.get("min") is None else _number(b.get("min"))
            if b.get("min") is not None and _number(b.get("min")) != 0:
                if mult_n is not None and min_n is not None:
                    out.set("delay", _fmt(max(mult_n, min_n)))
                else:
                    mult_t = "1" if mult is None else self.text(mult)
                    out.set("delay", f"max({mult_t}, {self.text(b['min'])})")
                out.notes.append(
                    "wait_exponential(min=...) is approximated: delay=max(multiplier, min) "
                    "grows by backoff from there, tenacity clamps every wait to min"
                )
            else:
                out.set("delay", "1" if mult is None else self.text(mult))
            exp_base = b.get("exp_base")
            out.set("backoff", "2" if exp_base is None else self.text(exp_base))
            if b.get("max") is not None:
                out.set("max_delay", self.text(b["max"]))
        else:  # wait_exponential_jitter
            if rand is not None:
                raise _Skip("wait_exponential_jitter cannot be combined with wait_random")
            out.set("delay", "1" if b.get("initial") is None else self.text(b["initial"]))
            out.set("backoff", "2" if b.get("exp_base") is None else self.text(b["exp_base"]))
            if b.get("max") is not None:
                out.set("max_delay", self.text(b["max"]))
            out.set("jitter", "1" if b.get("jitter") is None else self.text(b["jitter"]))
        if rand is not None:
            if leaf == "wait_fixed":
                # fixed + random(a, b): delay = s + a, jitter = b - a
                if rand_min is not None and _number(rand_min) != 0:
                    fixed = out.values["delay"]
                    fx, rn = _number(b["wait"]), _number(rand_min)
                    out.values["delay"] = (
                        _fmt(fx + rn)
                        if fx is not None and rn is not None
                        else f"{fixed} + {self.text(rand_min)}"
                    )
            elif rand_min is not None and _number(rand_min) != 0:
                raise _Skip("wait_random with a nonzero minimum added to a backoff strategy")
            out.set("jitter", jitter_from_random or "0")

    def _retry_cond(self, node: ast.expr | None, out: _Settings) -> None:
        if node is None:
            return
        if self._is_name(node, "retry_if_exception_type"):
            return
        parts: list[ast.expr]
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.BitAnd)):
            parts = _flatten(node, type(node.op))
            kind = "or" if isinstance(node.op, ast.BitOr) else "and"
        else:
            parts, kind = [node], "single"
        seen: dict[str, ast.Call] = {}
        for part in parts:
            found = self.call_of(part, "tenacity")
            if found is None:
                raise _Skip("retry condition not understood")
            leaf, call = found
            if (
                leaf
                not in (
                    "retry_if_exception_type",
                    "retry_if_result",
                    "retry_if_exception",
                )
                or leaf in seen
            ):
                raise _Skip(f"retry condition {leaf} is not supported")
            seen[leaf] = call
        if kind == "or" and set(seen) != {"retry_if_exception_type", "retry_if_result"}:
            raise _Skip("only `retry_if_exception_type(...) | retry_if_result(...)` is supported")
        if kind == "and" and set(seen) != {"retry_if_exception_type", "retry_if_exception"}:
            raise _Skip(
                "only `retry_if_exception_type(...) & retry_if_exception(...)` is supported"
            )
        if "retry_if_exception_type" in seen:
            bound = _bind(seen["retry_if_exception_type"], ("exception_types",))
            if "exception_types" in bound:
                out.set("on", self.text(bound["exception_types"]))
        if "retry_if_exception" in seen:
            bound = _bind(seen["retry_if_exception"], ("predicate",))
            if "predicate" not in bound:
                raise _Skip("retry_if_exception needs a predicate")
            out.set("retry_if", self.text(bound["predicate"]))
        if "retry_if_result" in seen:
            bound = _bind(seen["retry_if_result"], ("predicate",))
            if "predicate" not in bound:
                raise _Skip("retry_if_result needs a predicate")
            out.set("retry_if_result", self.text(bound["predicate"]))
            if kind == "single":
                out.set("on", "()")  # tenacity does not retry exceptions in this mode
        if kind == "single" and "retry_if_exception" in seen:
            pass  # on= stays the default Exception, same as tenacity's behaviour
        if "retry_if_result" in seen:
            out.notes.append(
                "when attempts run out python_wrap returns the last rejected value; tenacity "
                "raised RetryError"
            )

    # -- backoff -------------------------------------------------------------------------

    def backoff(self, deco: ast.expr, leaf: str) -> _Settings:
        out = _Settings()
        if not isinstance(deco, ast.Call):
            raise _Skip(f"backoff.{leaf} needs arguments")
        call = deco
        extra_ok = {"expo": ("factor", "base", "max_value"), "constant": ("interval",)}
        positional = ("wait_gen", "exception") if leaf == "on_exception" else ("wait_gen",)
        wait_gen = call.args[0] if call.args else None
        bound: dict[str, ast.expr] = {}
        wait_kwargs: dict[str, ast.expr] = {}
        known = {
            "wait_gen",
            "exception",
            "predicate",
            "max_tries",
            "max_time",
            "jitter",
            "giveup",
            "on_success",
            "on_backoff",
            "on_giveup",
            "raise_on_giveup",
            "logger",
            "backoff_log_level",
            "giveup_log_level",
            "max_value",
            "factor",
            "base",
            "interval",
        }
        if len(call.args) > len(positional) and leaf == "on_exception":
            raise _Skip("unsupported positional arguments")
        if leaf == "on_predicate" and len(call.args) > 2:
            raise _Skip("unsupported positional arguments")
        names = ("wait_gen", "predicate") if leaf == "on_predicate" else positional
        for i, arg in enumerate(call.args):
            if isinstance(arg, ast.Starred):
                raise _Skip("unsupported positional arguments")
            bound[names[i]] = arg
        for kw in call.keywords:
            if kw.arg is None:
                raise _Skip("**kwargs are not supported")
            if kw.arg not in known:
                raise _Skip(f"wait_gen argument {kw.arg!r} is not supported")
            if kw.arg in ("factor", "base", "max_value", "interval"):
                wait_kwargs[kw.arg] = kw.value
            else:
                bound[kw.arg] = kw.value
        wait_gen = bound.get("wait_gen")
        if wait_gen is None:
            raise _Skip("wait_gen not given")
        gen = self.names.leaf(wait_gen) if self.names.library(wait_gen) == "backoff" else None
        if gen not in extra_ok:
            raise _Skip("only backoff.expo and backoff.constant are supported")
        assert gen is not None
        for k in wait_kwargs:
            if k not in extra_ok[gen]:
                raise _Skip(f"argument {k!r} does not apply to backoff.{gen}")
        for key in ("on_success", "on_backoff", "on_giveup", "giveup"):
            if key in bound:
                raise _Skip(f"{key}= has no direct equivalent; use on_retry= or retry_if= by hand")
        for key in ("backoff_log_level", "giveup_log_level"):
            if key in bound:
                raise _Skip(f"{key}= has no direct equivalent; see log_level= and logger=")
        rg = bound.get("raise_on_giveup")
        if rg is not None and not (isinstance(rg, ast.Constant) and rg.value is True):
            raise _Skip("raise_on_giveup=False has no equivalent; python_wrap always re-raises")
        if leaf == "on_exception":
            if "exception" not in bound:
                raise _Skip("exception argument missing")
            out.set("on", self.text(bound["exception"]))
        else:
            pred = bound.get("predicate")
            out.set("on", "()")
            out.set(
                "retry_if_result", "lambda result: not result" if pred is None else self.text(pred)
            )
        mt = bound.get("max_tries")
        if mt is None or (isinstance(mt, ast.Constant) and mt.value is None):
            out.set("max_attempts", "None")
        elif isinstance(mt, (ast.Lambda, ast.Call)) and not isinstance(mt, ast.Constant):
            raise _Skip("callable max_tries is not supported")
        else:
            out.set("max_attempts", self.text(mt))
        mtm = bound.get("max_time")
        if mtm is not None and not (isinstance(mtm, ast.Constant) and mtm.value is None):
            if isinstance(mtm, (ast.Lambda, ast.Call)):
                raise _Skip("callable max_time is not supported")
            out.set("max_time", self.text(mtm))
        if gen == "expo":
            out.set(
                "delay", "1" if "factor" not in wait_kwargs else self.text(wait_kwargs["factor"])
            )
            out.set("backoff", "2" if "base" not in wait_kwargs else self.text(wait_kwargs["base"]))
            mv = wait_kwargs.get("max_value")
            if mv is not None and not (isinstance(mv, ast.Constant) and mv.value is None):
                out.set("max_delay", self.text(mv))
        else:
            out.set(
                "delay",
                "1" if "interval" not in wait_kwargs else self.text(wait_kwargs["interval"]),
            )
            out.set("backoff", "1")
        jit = bound.get("jitter")
        if jit is None or (
            self.names.library(jit) == "backoff" and self.names.leaf(jit) == "full_jitter"
        ):
            out.notes.append(
                "backoff's default full_jitter randomises the whole wait; python_wrap adds "
                "up to jitter= seconds on top. No jitter was added; set jitter= if you want some"
            )
        elif isinstance(jit, ast.Constant) and jit.value is None:
            pass
        else:
            raise _Skip("custom jitter functions are not supported")
        lg = bound.get("logger")
        if lg is not None:
            out.values["logger"] = self.text(lg)
        return out


# ---------------------------------------------------------------------------- the driver


def _fn_decorators(tree: ast.Module) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.extend(node.decorator_list)
    return found


def _refs(tree: ast.Module, names: _Names, skip: set[int]) -> list[ast.expr]:
    """Remaining references to tenacity/backoff names outside ``skip`` nodes."""
    out: list[ast.expr] = []

    def visit(node: ast.AST) -> None:
        if id(node) in skip:
            return
        if (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(node.ctx, ast.Load)
            and names.resolve(node) is not None
        ):
            out.append(node)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in tree.body:
        if stmt in names.imports:
            continue
        visit(stmt)
    return out


def _module_bindings(tree: ast.Module, names: _Names) -> set[str]:
    bound: set[str] = set()
    for stmt in tree.body:
        if stmt in names.imports:
            continue
        for node in (
            ast.walk(stmt)
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [stmt]
        ):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    bound.add((a.asname or a.name).split(".")[0])
    return bound


def migrate_source(text: str, filename: str = "<string>") -> MigrationResult:
    """Rewrite tenacity/backoff retry decorators in ``text`` to ``python_wrap.retry``.

    Args:
        text: Python source code.
        filename: Used only in parse error messages.

    Returns:
        A `MigrationResult`. ``text`` equals the input when nothing was rewritten or when
        the input does not parse (reported as an `Unsupported` finding).

    Example:
        >>> migrate_source("x = 1\\n").text
        'x = 1\\n'
    """
    result = MigrationResult(text)
    try:
        tree = ast.parse(text, filename=filename)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None) or 1
        col = max((getattr(exc, "offset", None) or 1) - 1, 0)
        result.unsupported.append(Unsupported(line, col, f"cannot parse file: {exc}"))
        return result
    names = _Names(tree)
    if not names.aliases and not names.star:
        return result
    src = _Source(text)
    tr = _Translator(src, names)
    for star in names.star:
        result.unsupported.append(
            Unsupported(star.lineno, star.col_offset, "star import: migrate this file by hand")
        )

    converted: list[tuple[ast.expr, _Settings]] = []
    skipped_nodes: set[int] = set()
    for deco in _fn_decorators(tree):
        target = deco.func if isinstance(deco, ast.Call) else deco
        lib, leaf = names.library(target), names.leaf(target)
        if lib is None or leaf is None:
            continue
        if (lib, leaf) == ("tenacity", "retry"):
            attempt: Callable[[], _Settings] = lambda d=deco: tr.tenacity(d)  # type: ignore[misc]  # noqa: E731
        elif lib == "backoff" and leaf in ("on_exception", "on_predicate"):
            attempt = lambda d=deco, k=leaf: tr.backoff(d, k)  # type: ignore[misc]  # noqa: E731
        else:
            continue
        try:
            converted.append((deco, attempt()))
        except _Skip as why:
            result.unsupported.append(Unsupported(deco.lineno, deco.col_offset, f"{why}"))
            skipped_nodes.update(id(n) for n in ast.walk(deco))
    if not converted:
        _report_leftovers(tree, names, skipped_nodes, result)
        result.unsupported.sort(key=lambda u: (u.line, u.col))
        return result

    converted_ids: set[int] = set()
    for deco, _ in converted:
        converted_ids.update(id(n) for n in ast.walk(deco))
    # Every reference that survives the rewrite (including inside unsupported decorators)
    # decides which tenacity/backoff imports must stay; only the ones outside unsupported
    # decorators still need a finding of their own.
    alive = _refs(tree, names, converted_ids)
    leftovers = _refs(tree, names, converted_ids | skipped_nodes)

    # Pick the name the new decorator is called by.
    bound = _module_bindings(tree, names)
    tenacity_retry_alive = any(
        names.library(r) == "tenacity" and names.leaf(r) == "retry" for r in alive
    )
    # a still-used tenacity `retry` import also collides with the new `retry`
    local_retry = [k for k, v in names.aliases.items() if v == "tenacity.retry"]
    clash = "retry" in bound or (tenacity_retry_alive and "retry" in local_retry)
    already = _has_python_wrap_retry(tree)
    if already:
        use_name, need_import = already, False
    else:
        use_name, need_import = ("python_wrap_retry" if clash else "retry"), True

    edits: list[tuple[int, int, str]] = []
    for deco, settings in converted:
        start, end = src.span(deco)
        indent = src.indent(deco.lineno)
        col = len(indent) + 1
        new = settings.render(use_name, indent, col)
        before = src.segment(deco)
        result.changes.append(
            Change(deco.lineno, deco.col_offset, before, new, tuple(settings.notes))
        )
        edits.append((start, end, new))

    # Imports.
    still_used: set[str] = set()
    for ref in alive:
        root: ast.expr = ref
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            still_used.add(root.id)
    for ref in leftovers:
        result.unsupported.append(
            Unsupported(
                ref.lineno,
                ref.col_offset,
                f"{names.resolve(ref)} is still used and has no automatic migration; "
                "see the migration guide",
            )
        )
    import_line = f"from python_wrap import retry{'' if use_name == 'retry' else ' as ' + use_name}"
    newline = "\r\n" if "\r\n" in text else "\n"
    pending_import = need_import
    first_import = True
    # statements sharing a line with another statement are left alone
    for stmt in names.imports:
        assert isinstance(stmt, (ast.Import, ast.ImportFrom))
        if _shares_line(tree, stmt):
            continue
        keep: list[str] = []
        if isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                local = alias.asname or alias.name
                if local in still_used:
                    keep.append(_alias_text(alias))
            kept_text = f"from {stmt.module} import {', '.join(keep)}" if keep else ""
            all_kept = len(keep) == len(stmt.names)
        else:
            for alias in stmt.names:
                root_name = (alias.asname or alias.name).split(".")[0]
                if (
                    alias.name.split(".")[0] not in ("tenacity", "backoff")
                    or root_name in still_used
                ):
                    keep.append(_alias_text(alias))
            kept_text = f"import {', '.join(keep)}" if keep else ""
            all_kept = len(keep) == len(stmt.names)
        if all_kept:
            kept_text = src.segment(stmt)
        pieces = [p for p in (kept_text,) if p]
        if first_import and pending_import:
            pieces.append(import_line)
            pending_import = False
        first_import = False
        s_start, s_end = src.span(stmt)
        indent = src.indent(stmt.lineno)
        replacement = newline.join(pieces[:1] + [indent + p for p in pieces[1:]]) if pieces else ""
        if not pieces:
            # drop the whole line(s) including the newline
            line_start = src.starts[stmt.lineno - 1]
            s_start = line_start
            s_end = _line_end_offset(src, stmt.end_lineno or stmt.lineno)
        edits.append((s_start, s_end, replacement))
    if pending_import:  # no module-level import statement could host it
        result.unsupported.append(
            Unsupported(1, 0, f"could not insert `{import_line}`; add it by hand")
        )

    new_text = text
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        new_text = new_text[:start] + repl + new_text[end:]
    try:
        ast.parse(new_text)
    except SyntaxError as exc:
        return MigrationResult(
            text,
            [],
            [*result.unsupported, Unsupported(exc.lineno or 1, 0, f"rewrite aborted: {exc.msg}")],
        )
    result.text = new_text
    result.changes.sort(key=lambda c: (c.line, c.col))
    result.unsupported.sort(key=lambda u: (u.line, u.col))
    return result


def _report_leftovers(
    tree: ast.Module, names: _Names, skip: set[int], result: MigrationResult
) -> None:
    for ref in _refs(tree, names, skip):
        result.unsupported.append(
            Unsupported(
                ref.lineno,
                ref.col_offset,
                f"{names.resolve(ref)} has no automatic migration; see the migration guide",
            )
        )


def _has_python_wrap_retry(tree: ast.Module) -> str | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "python_wrap" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name == "retry":
                    return alias.asname or "retry"
    return None


def _alias_text(alias: ast.alias) -> str:
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def _line_end_offset(src: _Source, lineno: int) -> int:
    return src.starts[lineno - 1] + len(src.lines[lineno - 1])


def _shares_line(tree: ast.Module, stmt: ast.stmt) -> bool:
    lo, hi = stmt.lineno, stmt.end_lineno or stmt.lineno
    for other in tree.body:
        if other is stmt:
            continue
        o_lo, o_hi = other.lineno, other.end_lineno or other.lineno
        if o_lo <= hi and lo <= o_hi:
            return True
    return False


# ---------------------------------------------------------------------------- CLI


def _iter_files(paths: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for found in sorted(path.rglob("*.py")):
                if not any(part in _SKIP_DIRS for part in found.relative_to(path).parts):
                    files.append(found)
        else:
            files.append(path)
    return files


def _read(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(iter(data.splitlines(keepends=True)).__next__)
    return data.decode(encoding), encoding


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for ``python -m python_wrap migrate``.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        0 when nothing needs manual attention, 1 when unsupported constructs were found,
        2 on usage or file errors.
    """
    parser = argparse.ArgumentParser(
        prog="python -m python_wrap migrate",
        description="Rewrite tenacity and backoff retry decorators to python_wrap.retry.",
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", help="files or directories")
    parser.add_argument("--write", action="store_true", help="write the rewritten files")
    parser.add_argument("--diff", action="store_true", help="print a unified diff")
    args = parser.parse_args(argv)

    unsupported_total = 0
    changed_files = 0
    errors = 0
    for path in _iter_files(args.paths):
        try:
            text, encoding = _read(path)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            print(f"{path}: cannot read: {exc}", file=sys.stderr)
            errors += 1
            continue
        result = migrate_source(text, str(path))
        for item in result.unsupported:
            print(f"{path}:{item.line}:{item.col + 1}: unsupported: {item.message}")
        unsupported_total += len(result.unsupported)
        if result.text == text:
            continue
        changed_files += 1
        for change in result.changes:
            print(f"{path}:{change.line}:{change.col + 1}: rewrote decorator")
            for note in change.notes:
                print(f"    note: {note}")
        if args.diff:
            sys.stdout.writelines(
                difflib.unified_diff(
                    text.splitlines(keepends=True),
                    result.text.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        if args.write:
            path.write_bytes(result.text.encode(encoding))
            print(f"{path}: written")
        else:
            print(f"{path}: would rewrite {len(result.changes)} decorator(s)")
    verb = "rewrote" if args.write else "would rewrite"
    print(f"{verb} {changed_files} file(s); {unsupported_total} unsupported finding(s)")
    if errors:
        return 2
    return 1 if unsupported_total else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
