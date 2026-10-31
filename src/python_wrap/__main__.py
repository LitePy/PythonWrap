"""Command line entry point: ``python -m python_wrap <command> ...``.

Commands:

* ``doctor TARGET...``: explain resilience stacks and flag risky combinations
  (see `python_wrap.doctor`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a subcommand and return its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m python_wrap {doctor} ..."
    if not args or args[0] in {"-h", "--help"}:
        print(usage)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "doctor":
        from python_wrap.doctor import main as run
    else:
        print(f"unknown command {command!r}\n{usage}", file=sys.stderr)
        return 2
    return run(rest)


if __name__ == "__main__":
    raise SystemExit(main())
