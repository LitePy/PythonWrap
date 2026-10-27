"""Fix the N+1 query problem with ``batch_calls`` (the DataLoader pattern).

Rendering 20 posts needs 20 authors. Written naively that is one query per post. With
``batch_calls`` every ``get_author(id)`` call made at about the same time is collected
into a single ``WHERE id IN (...)`` query, and callers still write simple per-item code.
The database is simulated with a list that records every query.

Run: python examples/dataloader.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from python_wrap.batching import batch_calls

QUERIES: list[str] = []
AUTHORS = {i: {"id": i, "name": f"author{i}"} for i in range(1, 6)}
POSTS = [{"id": n, "author_id": n % 5 + 1, "title": f"post {n}"} for n in range(20)]


@batch_calls(max_size=100, max_wait=0.005)
async def get_author(ids: list[int]) -> dict[int, dict[str, Any]]:
    """The one place that talks to the database. Receives distinct ids."""
    QUERIES.append(f"SELECT * FROM authors WHERE id IN ({', '.join(map(str, ids))})")
    await asyncio.sleep(0.001)
    return {i: AUTHORS[i] for i in ids if i in AUTHORS}  # missing ids get a BatchError


async def render_post(post: dict[str, Any]) -> str:
    author = await get_author(post["author_id"])  # looks like one query per post...
    return f"{post['title']} by {author['name']}"


async def render_all(posts: list[dict[str, Any]]) -> list[str]:
    return list(await asyncio.gather(*(render_post(p) for p in posts)))  # ...but is 1 in total


# --- demo ---


async def amain() -> None:
    lines = await render_all(POSTS)
    assert lines[0] == "post 0 by author1"
    print(len(lines), "posts rendered with", len(QUERIES), "query:")
    print(" ", QUERIES[0])
    assert len(QUERIES) == 1  # 20 posts, 5 distinct authors, one query

    # A second wave goes out as a second batch.
    await asyncio.gather(get_author(1), get_author(2))
    assert len(QUERIES) == 2


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
