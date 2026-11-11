"""Command line entry point: ``python -m python_wrap <command> ...``.

Commands:

* ``doctor TARGET...``: explain resilience stacks and flag risky combinations
  (see `python_wrap.doctor`).
* ``migrate PATH...``: rewrite tenacity/backoff decorators to python-wrap
  (see `python_wrap.migrate`).
* ``api [SYMBOL | search WORDS...] [--json]``: the public API index
  (see `python_wrap.introspect`).
* ``recipe NEED...``: which API solves a plain-language need.
* ``guide``: how to use python-wrap correctly (for people and coding agents).
* ``mcp``: a read-only Model Context Protocol server over stdio (see `python_wrap.mcp`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_USAGE = "usage: python -m python_wrap {doctor,migrate,api,recipe,guide,mcp} ..."


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a subcommand and return its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_USAGE)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "doctor":
        from python_wrap.doctor import main as doctor

        return doctor(rest)
    if command == "migrate":
        from python_wrap.migrate import main as migrate

        return migrate(rest)
    if command in {"api", "recipe", "guide"}:
        from python_wrap import introspect

        handler = {
            "api": introspect.api_main,
            "recipe": introspect.recipe_main,
            "guide": introspect.guide_main,
        }[command]
        return handler(rest)
    if command == "mcp":
        from python_wrap.mcp import main as mcp

        return mcp(rest)
    print(f"unknown command {command!r}\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
