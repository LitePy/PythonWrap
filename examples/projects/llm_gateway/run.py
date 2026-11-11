"""Entry point: ``python examples/projects/llm_gateway/run.py`` serves a few fake requests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway_app.app import build_gateway


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        gateway, _ = build_gateway(Path(folder) / "audit.jsonl")
        print(gateway.chat("acme", "What is a bulkhead?"))
        print(gateway.chat("acme", "What is a bulkhead?"), "(cached)")
        print("".join(gateway.stream("acme", "Say hi")))
        for finding in gateway.doctor_findings("acme"):
            print(finding)
        lines = (Path(folder) / "audit.jsonl").read_text().splitlines()
        print(f"audit tape: {len(lines)} record(s)")
        assert len(lines) == 1  # the repeat was a cache hit, so only one call was traced
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
