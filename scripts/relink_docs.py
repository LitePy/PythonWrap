"""One-off helper for the docs restructure: rewrite links to moved pages.

Usage: ``python scripts/relink_docs.py docs/a.md docs/b.md ...`` (default: every page).
Each relative link to an old page is replaced by a link to its new home, computed
relative to the page that contains it. An anchor is kept only when the target page has
a heading that produces it; otherwise the link points at the page itself.
"""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# old page -> new page, with anchor-specific overrides as "old.md#anchor".
MOVES: dict[str, str] = {
    "choosing.md": "get-started/choosing.md",
    "async.md": "get-started/async-and-methods.md",
    "stacking.md": "get-started/stacking.md",
    "testing.md": "testing/index.md",
    "performance.md": "project/performance.md",
    "limitations.md": "project/limitations.md",
    "migration.md": "project/migration.md",
    "changelog.md": "project/changelog.md",
    "guides/resilience.md": "features/resilience/index.md",
    "guides/resilience.md#retry": "features/resilience/retry.md",
    "guides/resilience.md#fallback": "features/resilience/fallback.md",
    "guides/resilience.md#suppress": "features/resilience/fallback.md",
    "guides/resilience.md#circuit-breaker": "features/resilience/circuit-breaker.md",
    "guides/resilience.md#timeout": "features/resilience/timeout.md",
    "guides/resilience.md#deadline": "features/resilience/timeout.md",
    "guides/policies.md": "features/resilience/policies.md",
    "guides/configuration.md": "features/configuration.md",
    "guides/caching.md": "features/caching/index.md",
    "guides/caching.md#memoize": "features/caching/memoize.md",
    "guides/caching.md#cache": "features/caching/memoize.md",
    "guides/caching.md#once": "features/caching/once.md",
    "guides/caching.md#cached_property": "features/caching/cached-property.md",
    "guides/batching.md": "features/caching/batching.md",
    "guides/batching.md#batch_calls": "features/caching/batching.md",
    "guides/batching.md#hedge": "features/resilience/hedge.md",
    "guides/concurrency.md": "features/concurrency/index.md",
    "guides/concurrency.md#rate_limit": "features/concurrency/rate-limits.md",
    "guides/concurrency.md#throttle": "features/concurrency/throttle-debounce.md",
    "guides/concurrency.md#debounce": "features/concurrency/throttle-debounce.md",
    "guides/concurrency.md#singleflight": "features/caching/singleflight.md",
    "guides/concurrency.md#run_in_thread": "features/concurrency/run-in-thread.md",
    "guides/async-toolkit.md": "features/concurrency/async-helpers.md",
    "guides/async-toolkit.md#bulkhead": "features/concurrency/bulkheads.md",
    "guides/async-toolkit.md#limit_concurrency": "features/concurrency/bulkheads.md",
    "guides/observability.md": "features/observability/index.md",
    "guides/observability.md#configuring-logging": "features/observability/logging.md",
    "guides/observability.md#log_args-and-log_return": "features/observability/logging.md",
    "guides/observability.md#trace": "features/observability/logging.md",
    "guides/observability.md#log_context": "features/observability/logging.md",
    "guides/observability.md#audit": "features/observability/audit.md",
    "guides/observability.md#timing": "features/observability/timing.md",
    "guides/observability.md#benchmark": "features/observability/timing.md",
    "guides/observability.md#measure_memory": "features/observability/timing.md",
    "guides/observability.md#profile": "features/observability/timing.md",
    "guides/metrics.md": "features/observability/metrics.md",
    "guides/validation.md": "features/correctness/validation.md",
    "guides/state.md": "features/correctness/state.md",
    "guides/devtools.md": "features/devtools.md",
}

# Pages that moved themselves: their links were written relative to the old folder.
ORIGIN: dict[str, str] = {
    "project/performance.md": "",
    "project/limitations.md": "",
    "project/migration.md": "",
    "project/changelog.md": "",
}

LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)\s]+?\.md)(#[^)\s]*)?\)")


def slugs(page: Path) -> set[str]:
    """Anchors mkdocs generates for the page's headings (toc default slugify)."""
    found: set[str] = set()
    if not page.exists():
        return found
    for line in page.read_text(encoding="utf-8").splitlines():
        m = re.match(r"#{1,6}\s+(.*?)\s*(\{[^}]*\})?\s*$", line)
        if m:
            text = re.sub(r"[`*_\[\]()]", lambda c: "_" if c.group() == "_" else "", m.group(1))
            text = re.sub(r"[^\w\s-]", "", text).strip().lower()
            found.add(re.sub(r"[\s]+", "-", text))
        for a in re.findall(r"\{[^}]*#([\w-]+)[^}]*\}", line):
            found.add(a)
    return found


def rewrite(page: Path) -> int:
    rel = page.relative_to(DOCS).as_posix()
    rel_dir = posixpath.dirname(rel)
    old_dir = ORIGIN.get(rel, rel_dir)
    text = page.read_text(encoding="utf-8")
    changes = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal changes
        target, anchor = m.group(1), m.group(2) or ""
        old = posixpath.normpath(posixpath.join(old_dir, target))
        new = MOVES.get(old + anchor) or MOVES.get(old) or old
        if new != old and anchor and anchor[1:] not in slugs(DOCS / new):
            anchor = ""
        link = f"]({posixpath.relpath(new, rel_dir or '.')}{anchor})"
        if link != m.group(0):
            changes += 1
        return link

    new_text = LINK.sub(sub, text)
    if new_text != text:
        page.write_text(new_text, encoding="utf-8")
    return changes


def main(argv: list[str]) -> int:
    pages = [Path(a).resolve() for a in argv] or sorted(DOCS.rglob("*.md"))
    for page in pages:
        n = rewrite(page)
        if n:
            print(f"{page.relative_to(DOCS)}: {n} link(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
