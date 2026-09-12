#!/usr/bin/env python3
"""Check the shared Paperless source and tests without copying deployment secrets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer", type=Path, help="Directory containing the other MCP pyproject.toml")
    args = parser.parse_args()
    local = Path(__file__).resolve().parents[1]
    peer = args.peer.resolve()
    failed = False
    for relative in (
        "heisenberg_access_mcp/paperless.py",
        "heisenberg_access_mcp/paperless_writes.py",
        "tests/test_paperless.py",
        "tests/test_paperless_writes.py",
        "scripts/check-shared-paperless.py",
    ):
        left, right = local / relative, peer / relative
        if not left.is_file() or not right.is_file():
            print(f"MISSING {relative}")
            failed = True
            continue
        content = left.read_bytes()
        if content != right.read_bytes():
            print(f"DIFFERENT {relative}")
            failed = True
            continue
        print(f"MATCH {hashlib.sha256(content).hexdigest()} {relative}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
