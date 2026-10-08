#!/usr/bin/env python3
"""Stamp docs/automation_status.json "lastSettle" as "checked, nothing pending to settle" -- used by the gated settle-only slots of auto-lock-settle.yml when
the gate finds nothing final to grade, so the header's LAST SETTLE stays fresh on quiet days (it only ever advanced when something was actually settled).
Writes only the lastSettle entry; never touches the ledger. Real (live) runs only -- the workflow step guards that.

    python3 scripts/settle_heartbeat.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as A  # noqa: E402


def main() -> int:
    A.write_automation_status("lastSettle", True, "settle check ran -- nothing pending to settle")
    print("lastSettle heartbeat written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
