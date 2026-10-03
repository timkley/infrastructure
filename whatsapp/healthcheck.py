"""Check local archive readiness without another WhatsApp connection.

An idle chat is healthy. This deliberately does not interpret the newest
message or HEARTBEAT timestamp as proof of connection health.
"""

import json
import os
from pathlib import Path
import subprocess


def main() -> int:
    try:
        result = subprocess.run(
            ["wacli", "--store", "/data/store", "--read-only", "--json", "doctor"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        envelope = json.loads(result.stdout)
        data = envelope["data"]
        if not envelope.get("success") or data.get("store_error"):
            return 1
        if not data.get("authenticated") or data.get("session_revoked"):
            return 1
        if not data.get("fts_enabled") or not data.get("lock_held"):
            return 1
        owner = data.get("lock_owner_pid")
        if not isinstance(owner, int) or owner <= 0:
            return 1
        os.kill(owner, 0)
        return 0 if Path(f"/proc/{owner}/comm").read_text().strip() == "wacli" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
