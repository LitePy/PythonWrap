"""A buffered writer that survives a crash, using a SQLite journal.

``buffered(journal=...)`` writes every accepted record to a journal before ``submit``
returns and removes it only after its batch was written. If the process dies (or the
batch handler keeps failing), the records stay in the journal and a restarted writer
replays them: delivery is at least once, so the handler must tolerate duplicates.

The "crash" here is a handler that fails and a writer that is abandoned without a
successful flush; the restart is a new writer on the same journal file.

Run: python examples/durable_buffer.py
"""

from __future__ import annotations

import os
import tempfile

from python_wrap.buffering import Buffered, SQLiteJournal


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "outbox.db")

        # Run 1: the destination is down, so no batch is ever written.
        def unavailable(rows: list[dict]) -> None:
            raise ConnectionError("destination down")

        first = Buffered(unavailable, flush_interval=0.01, journal=SQLiteJournal(path))
        for n in range(3):
            assert first.submit({"event": "signup", "id": n})
        first.close(timeout=2)  # simulate the crash: nothing was delivered
        first.journal.close()

        leftover = SQLiteJournal(path)
        assert len(leftover.pending()) == 3
        print(f"after the crash: {len(leftover.pending())} records still journaled")
        leftover.close()

        # Run 2: the process restarts with a working handler and replays the journal.
        delivered: list[dict] = []
        journal = SQLiteJournal(path)
        second = Buffered(delivered.extend, flush_interval=0.01, journal=journal)
        assert second.replay() == 3
        assert second.close(timeout=2)

        assert sorted(row["id"] for row in delivered) == [0, 1, 2]
        assert journal.pending() == []
        print(f"after the restart: delivered {len(delivered)}, journal empty")
        journal.close()


if __name__ == "__main__":
    main()
