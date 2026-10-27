"""Run every ```python block found in docs/ and fail if any of them raises.

Each block runs in a fresh namespace, so every example must be self-contained. A block
preceded by a line containing only ``<!-- skip -->`` is not run; one preceded by
``<!-- unix-only -->`` is skipped on Windows. Each block runs as a real module registered in
``sys.modules`` (so dataclasses, pickling and ``typing.get_type_hints`` work). Blocks inside tabs or
admonitions (indented fences) are dedented first. ``docs/api`` is generated from docstrings
and is not scanned.

Usage:
    uv run python scripts/check_docs_examples.py [path ...]
"""

from __future__ import annotations

import sys
import textwrap
import time
import traceback
import types
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SKIP_MARK = "<!-- skip -->"
UNIX_ONLY_MARK = "<!-- unix-only -->"


def extract(text: str) -> list[tuple[int, str, bool]]:
    """Return ``(first_line_number, source, skipped)`` for every python fence."""
    lines = text.splitlines()
    blocks: list[tuple[int, str, bool]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("```python"):
            indent = len(lines[i]) - len(stripped)
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            mark = lines[j].strip() if j >= 0 else ""
            skipped = mark == SKIP_MARK or (mark == UNIX_ONLY_MARK and sys.platform == "win32")
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                body.append(lines[i])
                i += 1
            source = textwrap.dedent("\n".join(body)) if indent else "\n".join(body)
            blocks.append((i - len(body) + 1, source + "\n", skipped))
        i += 1
    return blocks


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(
        p for p in DOCS.rglob("*.md") if "api" not in p.relative_to(DOCS).parts
    )
    ran = failed = skipped_count = 0
    started = time.perf_counter()
    for path in paths:
        for line, source, skipped in extract(path.read_text(encoding="utf-8")):
            where = f"{path.relative_to(ROOT) if path.is_absolute() else path}:{line}"
            if skipped:
                skipped_count += 1
                continue
            ran += 1
            name = f"_docs_example_{ran}"
            module = types.ModuleType(name)
            module.__file__ = where
            sys.modules[name] = module
            try:
                code = compile(source, where, "exec")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    exec(code, module.__dict__)
            except BaseException:
                failed += 1
                print(f"FAILED {where}", file=sys.stderr)
                traceback.print_exc()
            finally:
                sys.modules.pop(name, None)
    elapsed = time.perf_counter() - started
    print(f"{ran} examples run, {skipped_count} skipped, {failed} failed in {elapsed:.2f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
