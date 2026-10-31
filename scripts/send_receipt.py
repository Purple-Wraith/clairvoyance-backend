#!/usr/bin/env python3
"""Sends ONE subscriber their access receipt (what is active for them now, the standard price for that bundle, each list's end date).

Run by .github/workflows/send-receipt.yml, which the local MAILING LIST admin server dispatches (the Mac has no Gmail password; GitHub Actions does).  The subscriber list is read from the private repo
(CV_PRIVATE_DIR), so the add must already have been synced.

    python3 scripts/send_receipt.py --email someone@example.com
    python3 scripts/send_receipt.py --email referrer@example.com --kind referral    # the "you have earned a free month" note a referrer gets
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _subscribers import send_receipt_email, send_referral_notice_email  # noqa: E402

EMAIL_RE = re.compile(r"^[^\s@<>\"',;]+@[^\s@<>\"',;]+\.[^\s@<>\"',;]+$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--kind", choices=("receipt", "referral"), default="receipt")
    args = ap.parse_args()
    email = args.email.strip().lower()
    if not EMAIL_RE.match(email):
        print(f"not a valid email address: {email!r}")
        return 2
    ok, msg = (send_referral_notice_email if args.kind == "referral" else send_receipt_email)(email)
    print(("sent: " if ok else "NOT sent: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
