"""Command line entry point: ``python -m python_wrap <command> ...``.

Commands:

* ``doctor TARGET...``: explain resilience stacks and flag risky combinations
  (see `python_wrap.doctor`).
* ``migrate PATH...``: rewrite tenacity/backoff decorators to python-wrap
  (see `python_wrap.migrate`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a subcommand and return its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m python_wrap {doctor,migrate} ..."
    if not args or args[0] in {"-h", "--help"}:
        print(usage)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "doctor":
        from python_wrap.doctor import main as doctor

        return doctor(rest)
    if command == "migrate":
        from python_wrap.migrate import main as migrate

        return migrate(rest)
    print(f"unknown command {command!r}\n{usage}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
